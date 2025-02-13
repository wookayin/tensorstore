# Copyright 2022 The TensorStore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Defines data structures representing a Bazel workspace."""

# pylint: disable=missing-function-docstring,relative-beyond-top-level,g-doc-args

import argparse
import importlib
import pathlib
import platform
import re
import shlex
from typing import Dict, List, Optional, Set, Tuple, Union

from .cmake_target import CMakeDepsProvider
from .cmake_target import CMakePackageDepsProvider
from .cmake_target import CMakeTarget
from .starlark.bazel_target import parse_absolute_target
from .starlark.bazel_target import RepositoryId
from .starlark.bazel_target import TargetId
from .starlark.provider import Provider
from .starlark.provider import TargetInfo

# Maps `platform.system()` value to Bazel host platform name for use in
# .bazelrc.
_PLATFORM_NAME_TO_BAZEL_PLATFORM = {
    "Windows": "windows",
    "FreeBSD": "freebsd",
    "OpenBSD": "openbsd",
    "Darwin": "macos",
    "Linux": "linux",
}


def _parse_bazelrc(path: str):
  options: Dict[str, List[str]] = {}
  for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
      continue
    parts = shlex.split(line)
    options.setdefault(parts[0], []).extend(parts[1:])
  return options


class Workspace:
  """Relevant state of entire Bazel workspace.

  This is initialized in the top-level build and loaded via pickle for
  sub-projects that require bazel_to_cmake, in order to allow sub-projects to
  access targets defined by the top-level project.

  The workspace depends on the particular CMake build configuration, which
  allows all `select` expressions to be fully evaluated.
  """

  def __init__(
      self,
      cmake_vars: Dict[str, str],
      save_workspace: Optional[str] = None,
  ):
    # Variables provided by CMake.
    self.cmake_vars = cmake_vars
    self.save_workspace = save_workspace
    # Maps bazel repo names to CMake project name/prefix.
    self.bazel_to_cmake_deps: Dict[RepositoryId, str] = {}
    self.repos: Dict[RepositoryId, Repository] = {}
    self.repo_cmake_packages: Set[str] = set()
    self.host_platform_name = _PLATFORM_NAME_TO_BAZEL_PLATFORM.get(
        platform.system())
    self.values: Set[Tuple[str, str]] = set()
    self.copts: List[str] = []
    self.cxxopts: List[str] = []
    self.cdefines: List[str] = []
    self.ignored_libraries: Set[TargetId] = set()
    self._modules: Set[str] = set()
    self._analyzed_targets: Dict[TargetId, TargetInfo] = {}

  def set_bazel_target_mapping(self,
                               target: Union[str, TargetId],
                               cmake_target: Optional[CMakeTarget],
                               cmake_package: Optional[str] = None):
    """Records a mapping from a Bazel target to a CMake target."""
    if not isinstance(target, TargetId):
      target = parse_absolute_target(str(target))
    providers: List[Provider] = []
    if cmake_target is not None:
      providers.append(CMakeDepsProvider([cmake_target]))
    if cmake_package is not None:
      providers.append(CMakePackageDepsProvider([cmake_package]))

    assert isinstance(target, TargetId)
    self._analyzed_targets[target] = TargetInfo(*providers)

  def ignore_library(self, target: TargetId) -> None:
    """Marks a bzl library to be ignored.

    When requested via a `load` call, a dummy object will be returned instead.

    Like all `Workspace` state, this also propagates to bazel_to_cmake
    invocations for dependencies.
    """
    assert isinstance(target, TargetId)
    self.ignored_libraries.add(target)

  def exclude_repo_targets(self, repository_id: RepositoryId):
    """Deletes any stored `TargetInfo` for targets under the specified repo.

    When loading a dependency, some target aliases may have been defined by the
    top-level project.  They must be removed in order to allow the real target
    to be analyzed.
    """
    targets_to_delete = [
        target for target in self._analyzed_targets
        if target.repository_id == repository_id
    ]
    for target in targets_to_delete:
      del self._analyzed_targets[target]

  def load_bazelrc(self, path: str) -> None:
    """Loads options from a `.bazelrc` file.

    This currently only uses `--define` options.
    """
    self.add_bazelrc(_parse_bazelrc(path))

  def add_bazelrc(self, options: Dict[str, List[str]]) -> None:
    """Updates options based on a parsed `.bazelrc` file.

    This currently only uses `--define` options.
    """
    build_options = []
    build_options.extend(options.get("build", []))
    if self.host_platform_name is not None:
      build_options.extend(options.get(f"build:{self.host_platform_name}", []))

    ap = argparse.ArgumentParser()
    ap.add_argument("--copt", action="append", default=[])
    ap.add_argument("--cxxopt", action="append", default=[])
    ap.add_argument("--define", action="append", default=[])
    args, _ = ap.parse_known_args(build_options)

    self.values.update(("define", x) for x in args.define)

    def filter_copts(opts):
      return [
          opt for opt in opts
          if re.match("^(?:[-/]std|-fdiagnostics-color=|[-/]D)", opt) is None
      ]

    copts = filter_copts(args.copt)
    cxxopts = filter_copts(args.cxxopt)
    self.copts.extend(copts)
    self.cxxopts.extend(cxxopts)

  def add_module(self, module_name: str):
    self._modules.add(module_name)

  def load_modules(self):
    """Load modules added by add_module."""
    for module_name in self._modules:
      if module_name.startswith("."):
        importlib.import_module(module_name, package=__package__)
      else:
        importlib.import_module(module_name)


class Repository:
  """Represents a single Bazel repository and corresponding CMake project.

  This associates the source and output directories with a repository.

  Each run of `bazel_to_cmake` operates on just a single primary repository, but
  may load bzl libraries for the top-level repository as well.

  The `Workspace` holds a mapping of repository names to `Repository` objects.
  In the top-level invocation of `bazel_to_cmake`, only the top-level repository
  is present.  When invoked on dependencies, both the top-level repository and
  the dependency currently being processed are present.
  """

  def __init__(
      self,
      workspace: Workspace,
      bazel_repo_name: str,
      cmake_project_name: str,
      cmake_binary_dir: str,
      source_directory: str,
      top_level: bool,
  ):
    assert workspace
    assert bazel_repo_name
    self.workspace = workspace
    self.repository_id = RepositoryId(bazel_repo_name)
    self.cmake_project_name = cmake_project_name
    self.cmake_binary_dir = str(pathlib.PurePath(cmake_binary_dir).as_posix())
    self.source_directory = str(pathlib.PurePath(source_directory).as_posix())
    self.repo_mapping: Dict[str, str] = {}
    self.top_level = top_level
    workspace.repos[self.repository_id] = self
    if top_level:
      workspace.repos[RepositoryId("")] = self
    workspace.repo_cmake_packages.add(cmake_project_name)

  def __repr__(self):
    return f"<{self.__class__.__name__}>: {self.__dict__}"

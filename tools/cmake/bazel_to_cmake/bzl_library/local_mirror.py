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
"""CMake implementation of "@com_google_tensorstore//bazel:local_mirror.bzl"."""

# pylint: disable=invalid-name,missing-function-docstring,relative-beyond-top-level,g-long-lambda

import io
import os
import pathlib

from ..cmake_builder import CMakeBuilder
from ..cmake_builder import FETCH_CONTENT_DECLARE_SECTION
from ..cmake_builder import quote_string
from ..evaluation import EvaluationState
from .helpers import update_target_mapping
from .helpers import write_bazel_to_cmake_cmakelists
from ..starlark.bazel_globals import BazelGlobals
from ..starlark.bazel_globals import register_bzl_library
from ..starlark.bazel_target import RepositoryId
from ..starlark.invocation_context import InvocationContext


@register_bzl_library(
    "@com_google_tensorstore//bazel:local_mirror.bzl", workspace=True)
class ThirdPartyLocalMirrorLibrary(BazelGlobals):

  def bazel_local_mirror(self, **kwargs):
    _local_mirror_impl(self._context, **kwargs)


def _local_mirror_impl(_context: InvocationContext, **kwargs):
  cmake_name = kwargs.get("cmake_name")
  if not cmake_name:
    return

  bazel_to_cmake = kwargs.get("bazel_to_cmake")
  if bazel_to_cmake is None:
    return

  builder = _context.access(CMakeBuilder)
  state = _context.access(EvaluationState)
  new_repository_id = RepositoryId(kwargs["name"])

  state.workspace.bazel_to_cmake_deps[new_repository_id] = cmake_name

  update_target_mapping(state.repo, new_repository_id.get_package_id(""),
                        kwargs)

  for lang in kwargs.pop("cmake_languages", []):
    builder.addtext(
        f"enable_language({lang})\n",
        section=FETCH_CONTENT_DECLARE_SECTION,
        unique=True,
    )

  # Implementation
  files = kwargs.get("files")
  if not files:
    return

  local_mirror_dir = os.path.join(state.repo.cmake_binary_dir, "local_mirror",
                                  cmake_name)
  os.makedirs(local_mirror_dir, exist_ok=True)

  # Augment the CMakeLists.txt file with file(DOWNLOAD).
  out = io.StringIO()
  file_content = kwargs.get("file_content", {})
  file_url = kwargs.get("file_url", {})
  file_sha256 = kwargs.get("file_sha256", {})

  for file in files:
    file_path = pathlib.Path(os.path.join(local_mirror_dir, file))
    content = file_content.get(file)
    if content is not None:
      os.makedirs(file_path.parent, exist_ok=True)
      file_path.write_text(content, encoding="utf-8")
      continue
    urls = file_url.get(file)
    if not urls:
      continue
    out.write(
        f"file(DOWNLOAD {quote_string(urls[0])} {quote_string(str(file_path))}")
    sha256 = file_sha256.get(file)
    if not sha256:
      raise ValueError(
          f"local_mirror requires SHA256 for downloaded file: {file}")
    out.write(f"""\n     EXPECTED_HASH "SHA256={sha256}")\n\n""")

  cmaketxt_path = pathlib.Path(os.path.join(local_mirror_dir, "CMakeLists.txt"))

  builder.addtext(
      f"# Loading {new_repository_id.repository_name}\n",
      section=FETCH_CONTENT_DECLARE_SECTION)
  builder.addtext(out.getvalue(), section=FETCH_CONTENT_DECLARE_SECTION)
  builder.addtext(
      f"add_subdirectory({quote_string(str(cmaketxt_path.parent))} EXCLUDE_FROM_ALL)\n",
      section=FETCH_CONTENT_DECLARE_SECTION)

  # Now write the nested CMakeLists.txt file
  out = io.StringIO()
  out.write(f'set(CMAKE_MESSAGE_INDENT "[{cmake_name}] ")\n')

  if kwargs.get("cmakelists_prefix"):
    out.write(str(kwargs.get("cmakelists_prefix")))

  write_bazel_to_cmake_cmakelists(
      _context=_context, _new_cmakelists=out, _patch_commands=[], **kwargs)

  if kwargs.get("cmakelists_suffix"):
    out.write(str(kwargs.get("cmakelists_suffix")))

  cmaketxt_path.write_text(out.getvalue(), encoding="utf-8")

  # Clients rely on find_package; provide a -config.cmake file
  # for that.
  cmake_find_package_redirects_dir = state.workspace.cmake_vars[
      "CMAKE_FIND_PACKAGE_REDIRECTS_DIR"]
  if (kwargs.get("cmake_package_redirect_extra") is not None or
      kwargs.get("cmake_package_aliases") is not None or
      kwargs.get("cmake_package_redirect_libraries") is not None):
    # No aliases, etc. allowed for local_mirror.
    raise ValueError("CMake options not supported by local_mirror")

  config_path = os.path.join(cmake_find_package_redirects_dir,
                             f"{cmake_name.lower()}-config.cmake")
  pathlib.Path(config_path).write_text(
      f"""
set({cmake_name.lower()}_ROOT_DIR {local_mirror_dir})
set({cmake_name.lower()}_FOUND ON)
set({cmake_name.upper()}_FOUND ON)
""",
      encoding="utf-8")

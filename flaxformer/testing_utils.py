# Copyright 2021 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test-only library."""

import json
import pathlib
import re
from typing import Any, Dict, Mapping

from absl.testing import absltest
from flax.core import frozen_dict
import jax

from flaxformer.architectures.common import param_remapping


def param_shapes(params: Mapping[str, Any]) -> Dict[str, Any]:
  """Converts a tree of params into a tree of param shapes."""
  params = param_remapping.filter_out_metadata(params)
  return jax.tree_map(lambda x: list(x.shape), frozen_dict.unfreeze(params))  # pytype: disable=wrong-arg-types


def param_dtypes_shapes(params: Mapping[str, Any]) -> Dict[str, Any]:
  """Converts a tree of params into a tree of param dtypes and shapes."""
  params = param_remapping.filter_out_metadata(params)
  return jax.tree_map(lambda x: [str(x.dtype)] + list(x.shape),
                      frozen_dict.unfreeze(params))  # pytype: disable=wrong-arg-types


def format_params_shapes(params_shapes: Dict[str, Any]) -> str:
  """Formats a dictionary of parameter shapes into a string.

  Args:
    params_shapes: Dictionary of parameter shapes.

  Returns:
    String formatted result of those parameter shapes, which is nicely formatted
      by using JSON / indentation, but formatting short lists into one line.
  """

  # Typically, shape arrays are very verbose, so we want to re-format them to
  # fit on a single line. Do so if it wouldn't overflow.
  def re_compact_arrays(array_match) -> str:
    try:
      values = json.loads(array_match.group(0))
    except ValueError:
      return array_match.group(0)
    re_compacted = json.dumps(values)  # no indent parameter
    return re_compacted if len(re_compacted) < 80 else array_match.group(0)

  json_formatted = json.dumps(params_shapes, indent=2)
  return re.sub(r'\[[^\[\]]+\]', re_compact_arrays, json_formatted)


class ExpectedJsonFiles:
  """Helps check param shapes against JSON files with expected values.

  The JSON files with expected shapes contain the parameter pytree, for example,

  "mlp": {
    "wi": {
      "kernel": [13, 2048]
    },
    "wo": {
      "kernel": [2048, 13]
    }
  },

  If the dtype is also included, then it is provided before the shape, e.g.
  `"kernel": ["float32", 13, 2048]`.

  If the shapes don't match, then the expected shape is printed out. For
  intentional changes / regression testing, it can be appropriate to copy this
  to the expected shape JSON file.
  """

  def __init__(self, base_path: str):
    self.path = pathlib.Path(absltest.get_default_test_srcdir()) / base_path

  def get_params(self, filename: str) -> Mapping[str, Any]:
    with open(self.path / filename) as f:
      return json.load(f)

  def check_params(
      self,
      actual_params: Mapping[str, Any],
      expected_filename: str,
  ) -> None:
    """Checks parameter dtypes and shapes against expected values."""
    actual = param_dtypes_shapes(actual_params)
    expected = self.get_params(expected_filename)

    if actual != expected:
      print(format_params_shapes(actual))
      raise AssertionError(
          f'Didn\'t match JSON params in {expected_filename}. See actual '
          'values above.')

  def check_params_shapes_only(
      self,
      actual_params: Mapping[str, Any],
      expected_filename: str,
  ) -> None:
    """Checks parameter shapes against expected values."""
    actual = param_shapes(actual_params)
    expected = self.get_params(expected_filename)

    if actual != expected:
      print(format_params_shapes(actual))
      raise AssertionError(
          f'Didn\'t match JSON params in {expected_filename}. See actual '
          'values above.')

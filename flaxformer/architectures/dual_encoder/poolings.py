# Copyright 2022 Google LLC.
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

"""Layers for pooling operations."""

from typing import Callable

from flax import linen as nn
from flax.linen.linear import default_kernel_init
import jax
from jax import lax
import jax.numpy as jnp

from flaxformer import activation_partitioning
from flaxformer.architectures.t5 import t5_common_layers
from flaxformer.components import dense
from flaxformer.components.attention import dense_attention
from flaxformer.types import Array
from flaxformer.types import DType
from flaxformer.types import Initializer

NEG_INF = -1e10
EPSILON = 1e-10


class AttentionPooling(nn.Module):
  """Self attention pooling given a sequence of encodings.

  Reference: https://arxiv.org/pdf/1712.02047.pdf.

  Attributes:
    kernel_init: Initializer for the dense layer kernel.
    dtype: The dtype of the computation (default: float32).
    act_fn: activation function.
  """
  kernel_init: Initializer = default_kernel_init
  dtype: DType = jnp.float32
  act_fn: str = 'linear'

  @nn.compact
  def __call__(self, encoded_inputs: Array, input_masks: Array, **kwargs):
    """Apply attention pooling to the encoder output embeddings.

    Args:
      encoded_inputs: The inputs (e.g., token's embeddings) that come from the
        final layer of the encoder. <float32>[batch_size, seq_length,
        hidden_size].
      input_masks: The input masks that indicate the non padding position of the
        sequences. <float32>[batch_size, seq_length].
      **kwargs: Keyward based arguments, currently unused.

    Returns:
      An array of logits <float32>[batch_size, hidden_size].
    """
    encoding_size = encoded_inputs.shape[-1]
    attention_hidden = dense.DenseGeneral(
        features=encoding_size,
        use_bias=True,
        dtype=self.dtype,
        kernel_init=self.kernel_init,
        kernel_axis_names=['embed', 'affinity'],
        name='attention_hidden')(
            encoded_inputs)
    if self.act_fn != 'linear':
      attention_hidden = getattr(nn, self.act_fn)(attention_hidden)
    attention_logits = dense.DenseGeneral(
        features=encoding_size,
        use_bias=True,
        dtype=self.dtype,
        kernel_init=self.kernel_init,
        kernel_axis_names=['embed', 'affinity'],
        name='attention_logits')(
            attention_hidden)
    # Broadcast to the `hidden_size` dimension.
    input_masks = jnp.expand_dims(input_masks, axis=-1)
    attention_bias = lax.select(
        input_masks > 0,
        jnp.full(input_masks.shape, 0.).astype(self.dtype),
        jnp.full(input_masks.shape, NEG_INF).astype(self.dtype))
    logits = attention_logits + attention_bias
    weights = jax.nn.softmax(logits, axis=1)
    encodings = jnp.sum(encoded_inputs * weights, axis=1)

    return encodings


class MultiHeadAttentionPooling(nn.Module):
  """Multihead attention pooling given a sequence of encodings.

  Implements multihead attention based pooling where query is a single vector
  and key/values are computed by projecting the encoded input.

  Attributes:
    num_heads: number of attention heads. Features (i.e. inputs_q.shape[-1])
      should be divisible by the number of heads.
    dropout_factory: A callable that returns the dropout layer.
    layer_norm_factory: A callable that returns a layer norm.
    activation_partitioning_dims: When set to 2, partitions intermediate
      variables containing the input and output of the encoder layer.
    query_init: Initializer for the query vector.
    dropout_rate: dropout rate
    dtype: The dtype of the computation (default: float32).
  """
  num_heads: int
  head_dim: int
  layer_norm_factory: Callable[[], nn.Module]
  activation_partitioning_dims: int = 1
  query_init: Initializer = nn.initializers.zeros
  dropout_rate: float = 0.1
  dtype: DType = jnp.float32

  @nn.compact
  def __call__(self,
               encoded_inputs: Array,
               input_masks: Array,
               deterministic: bool = False):
    """Apply attention pooling to the encoder output embeddings.

    Args:
      encoded_inputs: The inputs (e.g., token's embeddings) that come from the
        final layer of the encoder. <float32>[batch_size, seq_length,
        hidden_size].
      input_masks: The input masks that indicate the non padding position of the
        sequences. <float32>[batch_size, seq_length].
      deterministic: Disables dropout if set to True.

    Returns:
      An array of logits <float32>[batch_size, hidden_size].
    """
    encoding_size = encoded_inputs.shape[-1]
    batch_size = encoded_inputs.shape[0]
    query = self.param('attention_query', self.query_init, (encoding_size,))

    # [batch_size, 1 embedding_size]
    query_3d = jnp.tile(query, (batch_size, 1, 1))
    query_3d = activation_partitioning.with_sharding(
        query_3d, self.activation_partitioning_dims)
    x = self.layer_norm_factory()(query_3d)
    x = activation_partitioning.with_sharding(x,
                                              self.activation_partitioning_dims)

    # Also see the `attention_layer` function defined in
    # flaxformer/architectures/t5/t5_common_layers.
    encoder_masks = dense_attention.make_attention_mask(
        jnp.ones([batch_size, 1], dtype=input_masks.dtype), input_masks)
    y = t5_common_layers.attention_layer(
        num_heads=self.num_heads,
        head_dim=self.head_dim,
        dropout_rate=self.dropout_rate,
        dtype=self.dtype)(
            x, encoded_inputs, encoder_masks, enable_dropout=not deterministic)

    y = nn.Dropout(
        rate=self.dropout_rate, broadcast_dims=[])(
            y, deterministic=deterministic)

    # [batch_size, 1, embedding_size]
    y = activation_partitioning.with_sharding(y,
                                              self.activation_partitioning_dims)
    return jnp.reshape(y, (batch_size, encoding_size))


class MeanPooling(nn.Module):
  """Mean pooling given a sequence of encodings."""

  @nn.compact
  def __call__(self, encoded_inputs: Array, input_masks: Array, **kwargs):
    """Apply mean pooling to the encoder output embeddings.

    Args:
      encoded_inputs: The inputs (e.g., token's embeddings) that come from the
        final layer of the encoder. <float32>[batch_size, seq_length,
        hidden_size].
      input_masks: The input masks that indicate the non padding position of the
        sequences. <float32>[batch_size, seq_length].
     **kwargs: Keyward based arguments, currently unused.

    Returns:
      An array of logits <float32>[batch_size, hidden_size].
    """
    # Broadcast to the `hidden_size` dimension.
    input_masks = jnp.expand_dims(input_masks, axis=-1)
    embeddings_sum = jnp.sum(encoded_inputs * input_masks, axis=1)
    masks_sum = jnp.maximum(input_masks.sum(axis=1), EPSILON)

    return embeddings_sum / masks_sum


class MaxPooling(nn.Module):
  """Max pooling given a sequence of encodings."""

  @nn.compact
  def __call__(self, encoded_inputs: Array, input_masks: Array, **kwargs):
    """Apply max pooling to the encoder output embeddings.

    Args:
      encoded_inputs: The inputs (e.g., token's embeddings) that come from the
        final layer of the encoder. <float32>[batch_size, seq_length,
        hidden_size].
      input_masks: The input masks that indicate the non padding position of the
        sequences. <float32>[batch_size, seq_length].
     **kwargs: Keyward based arguments, currently unused.

    Returns:
      An array of logits <float32>[batch_size, hidden_size].
    """
    # Broadcast to the `hidden_size` dimension.
    input_masks = jnp.expand_dims(input_masks, axis=-1)
    encodings = encoded_inputs * input_masks + (1 - input_masks) * -1e9
    encodings = jnp.max(encodings, 1)

    return encodings

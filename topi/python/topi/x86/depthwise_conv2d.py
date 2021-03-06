# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name,unused-variable,unused-argument,no-member
# pylint: disable=no-value-for-parameter
"""Depthwise Conv2D schedule on x86"""
import tvm
from tvm import autotvm
from tvm.autotvm.task.space import SplitEntity
from ..nn.pad import pad
from ..util import get_const_tuple
from ..nn.util import get_pad_tuple
from ..nn.depthwise_conv2d import _get_workload, depthwise_conv2d_infer_layout
from ..nn.conv2d import unpack_NCHWc_to_nchw
from ..util import traverse_inline
from .util import get_fp32_len

def _fallback_schedule(cfg, wkl):
    """
    Get default schedule for the workload
    Parameters
    ----------
    cfg : tvm.autotvm.task.space.FallbackConfigEntity
        Fallback config to be updated
    wkl : topi.nn.depthwise_conv2d.Workload
        Convolution workload
    """
    simd_width = get_fp32_len()

    HPAD, WPAD = wkl.hpad, wkl.wpad
    HSTR, WSTR = wkl.hstride, wkl.wstride
    out_height = (wkl.height + 2 * HPAD - wkl.hkernel) // HSTR + 1
    out_width = (wkl.width + 2 * WPAD - wkl.wkernel) // WSTR + 1

    oc_bn = 1
    for bn in range(simd_width, 0, -1):
        if wkl.out_filter % bn == 0:
            oc_bn = bn
            break

    ic_bn = 1
    for bn in range(oc_bn, 0, -1):
        if wkl.in_filter % bn == 0:
            ic_bn = bn
            break

    reg_n = 1
    for n in range(31, 0, -1):
        if out_width % n == 0:
            reg_n = n
            break

    cfg["tile_ic"] = SplitEntity([wkl.in_filter // ic_bn, ic_bn])
    cfg["tile_oc"] = SplitEntity([wkl.out_filter // oc_bn, oc_bn])
    cfg["tile_ow"] = SplitEntity([out_width // reg_n, reg_n])

def depthwise_conv2d_nchw(data, kernel, strides, padding, dilation, out_dtype):
    """Compute depthwise conv2d with NCHW layout."""
    layout = "NCHW"
    packed_out = depthwise_conv2d_NCHWc(data, kernel, strides, padding, dilation,
                                        layout, layout, out_dtype)
    return unpack_NCHWc_to_nchw(packed_out, out_dtype)

def schedule_depthwise_conv2d_nchw(outs):
    """Create schedule for depthwise_conv2d_nchw."""
    return schedule_depthwise_conv2d_NCHWc(outs)

def _pack_data(cfg, data, kernel):
    n, ic, ih, iw = get_const_tuple(data.shape)
    filters, cm, kh, kw = get_const_tuple(kernel.shape)
    oc = filters * cm
    ic_bn, oc_bn = cfg["tile_ic"].size[-1], cfg["tile_oc"].size[-1]

    ic_chunk = ic // ic_bn
    oc_chunk = oc // oc_bn

    data = tvm.compute((n, ic_chunk, ih, iw, ic_bn),
                       lambda bs, c, h, w, vc: data[bs, c*ic_bn + vc, h, w],
                       name="data_vec")

    kernel = tvm.compute(
        (oc_chunk, 1, kh, kw, 1, oc_bn),
        lambda occ, icc, k_h, k_w, icb, ocb:
        kernel[(occ * oc_bn + ocb) // cm,
               (occ * oc_bn + ocb) % cm, k_h, k_w],
        name="kernel_vec")

    return data, kernel

@autotvm.register_topi_compute("depthwise_conv2d_NCHWc.x86")
def depthwise_conv2d_NCHWc(cfg, data, kernel, strides, padding, dilation,
                           layout, out_layout, out_dtype=None):
    """Compute depthwise conv2d with NCHWc layout"""
    out_dtype = data.dtype if out_dtype is None else out_dtype

    if len(data.shape) == 5:
        batch, in_channel_chunk, in_height, in_width, in_channel_block = get_const_tuple(data.shape)
        out_channel_chunk, cm_chunk, filter_height, filter_width, cm_block, out_channel_block \
            = get_const_tuple(kernel.shape)
        in_channel = in_channel_chunk * in_channel_block
        out_channel = out_channel_chunk * out_channel_block
        channel_multiplier = cm_chunk * cm_block
        assert channel_multiplier * in_channel == out_channel
    else:
        batch, in_channel, in_height, in_width = get_const_tuple(data.shape)
        out_channel, channel_multiplier, filter_height, filter_width = get_const_tuple(kernel.shape)
    assert channel_multiplier == 1

    strides = strides if isinstance(strides, (tuple, list)) else (strides, strides)
    HSTR, WSTR = strides
    pad_top, pad_left, pad_down, pad_right = get_pad_tuple(padding, (filter_height, filter_width))

    dh, dw = dilation if isinstance(dilation, (tuple, list)) else (dilation, dilation)
    assert (dh, dw) == (1, 1), "Does not support dilation"

    out_height = (in_height - filter_height + pad_top + pad_down) // HSTR + 1
    out_width = (in_width - filter_width + pad_left + pad_right) // WSTR + 1

    cfg.define_split("tile_ic", in_channel, num_outputs=2)
    cfg.define_split("tile_oc", out_channel, num_outputs=2)
    cfg.define_split("tile_ow", out_width, num_outputs=2, filter=lambda y: y.size[-1] <= 64)

    # get workload and related schedule config
    wkl = _get_workload(
        tvm.placeholder((batch, in_channel, in_height, in_width), dtype=data.dtype),
        tvm.placeholder((out_channel, channel_multiplier, filter_height, filter_width),
                        dtype=kernel.dtype),
        strides, padding, out_dtype)
    if cfg.is_fallback:
        _fallback_schedule(cfg, wkl)

    # Pack data if raw 4-D data is provided.
    # This can only happen when autotuning.
    if len(data.shape) == 4:
        data, kernel = _pack_data(cfg, data, kernel)
        _, _, _, _, in_channel_block = get_const_tuple(data.shape)
        out_channel_chunk, _, _, _, _, out_channel_block \
            = get_const_tuple(kernel.shape)

    # padding stage
    DOPAD = (pad_top != 0 or pad_left != 0 or pad_down != 0 or pad_right != 0)
    if DOPAD:
        pad_before = [0, 0, pad_top, pad_left, 0]
        pad_after = [0, 0, pad_down, pad_right, 0]
        data_pad = pad(data, pad_before, pad_after, name="PaddedInput")
    else:
        data_pad = data

    # depthconv stage
    idxdiv = tvm.indexdiv
    idxmod = tvm.indexmod

    kh = tvm.reduce_axis((0, filter_height), name='kh')
    kw = tvm.reduce_axis((0, filter_width), name='kw')
    Output = tvm.compute(
        (batch, out_channel_chunk, out_height, out_width, out_channel_block),
        lambda b, oco, oh, ow, oci: tvm.sum(
            (data_pad[
                b,
                idxdiv(idxdiv(oco * out_channel_block + oci, channel_multiplier), in_channel_block),
                oh*HSTR+kh, ow*WSTR+kw,
                idxmod(idxdiv(oco * out_channel_block + oci, channel_multiplier), in_channel_block)]
             .astype(out_dtype) *
             kernel[oco, 0, kh, kw, 0, oci].astype(out_dtype)),
            axis=[kh, kw]),
        name='DepthwiseConv2d', tag="depthwise_conv2d_NCHWc")
    return Output

@autotvm.register_topi_schedule("depthwise_conv2d_NCHWc.x86")
def schedule_depthwise_conv2d_NCHWc(cfg, outs):
    """CPU schedule for depthwise conv2d in NCHW[x]c layout"""
    outs = [outs] if isinstance(outs, tvm.tensor.Tensor) else outs
    s = tvm.create_schedule([x.op for x in outs])

    def _callback(op):
        """Traverse operators from computation graph"""
        if 'depthwise_conv2d_NCHWc' in op.tag:
            conv_out = op.output(0)
            data = conv_out.op.input_tensors[0]
            kernel = conv_out.op.input_tensors[1]
            _schedule_depthwise_conv2d_NCHWc_impl(s, cfg, data, kernel, conv_out, outs[0])

    traverse_inline(s, outs[0].op, _callback)
    return s

def _schedule_depthwise_conv2d_NCHWc_impl(s, cfg, data_vec, kernel_vec, conv_out, output):
    tile_ow, oc_bn = cfg["tile_ow"].size[-1], cfg["tile_oc"].size[-1]
    # schedule pad
    if isinstance(s[data_vec].op, tvm.tensor.ComputeOp) \
            and "pad" in data_vec.op.tag:
        batch, ic_chunk, ih, iw, ic_block = s[data_vec].op.axis
        parallel_axis = s[data_vec].fuse(batch, ic_chunk, ih)
        s[data_vec].parallel(parallel_axis)
        data_vec = data_vec.op.input_tensors[0]

    if autotvm.GLOBAL_SCOPE.in_tuning:
        # only in autotuning, input data of conv2d_NCHWc will be 4-D.
        # skip this part during tuning to make recrods accurate.
        # this part will be folded during Relay fold_constant pass.
        s[data_vec].pragma(s[data_vec].op.axis[0], "debug_skip_region")
        s[kernel_vec].pragma(s[kernel_vec].op.axis[0], "debug_skip_region")

    C, O = conv_out, output
    CC = s.cache_write(C, 'global')

    _, ic_chunk, oh, ow, ic_block = s[C].op.axis
    ow_chunk, ow_block = s[C].split(ow, factor=tile_ow)
    s[C].reorder(ic_chunk, oh, ow_chunk, ow_block, ic_block)
    parallel_axis = s[C].fuse(ic_chunk, oh)
    s[C].parallel(parallel_axis)
    s[CC].compute_at(s[C], ow_chunk)

    # the ow axis in the cached block CC is the ow_block in C
    _, ic_chunk, oh, ow, ic_block = s[CC].op.axis
    kh, kw = s[CC].op.reduce_axis
    s[CC].reorder(ic_chunk, oh, kh, kw, ow, ic_block)
    s[CC].vectorize(ic_block)
    s[CC].unroll(ow)

    if C != O:
        out_ndim = len(s[O].op.axis)
        if out_ndim == 5:
            batch, oc_chunk, oh, ow, oc_block = s[O].op.axis
            ow_chunk, ow_block = s[O].split(ow, factor=tile_ow)
            s[O].reorder(oc_chunk, oh, ow_chunk, ow_block, oc_block)
            parallel_axis = s[O].fuse(oc_chunk, oh)
            s[C].compute_at(s[O], parallel_axis)
            s[O].vectorize(oc_block)
            s[O].parallel(parallel_axis)
        elif out_ndim == 4:
            batch, oc, oh, ow = s[O].op.axis
            ow_chunk, ow_block = s[O].split(ow, factor=tile_ow)
            oc_chunk, oc_block = s[O].split(oc, factor=oc_bn)
            s[O].reorder(oc_chunk, oh, ow_chunk, ow_block, oc_block)
            parallel_axis = s[O].fuse(oc_chunk, oh)
            s[C].compute_at(s[O], parallel_axis)
            s[O].vectorize(oc_block)
            s[O].parallel(parallel_axis)
        else:
            raise ValueError("Unsupported output ndim: %s" % out_ndim)

    return s

@depthwise_conv2d_infer_layout.register("cpu")
def _depthwise_conv2d_infer_layout(workload, cfg):
    _, data, kernel, strides, padding, dilation, dtype = workload
    batch_size, in_channel, in_height, in_width = data[1]
    filter_channel, channel_multiplier, k_height, k_width = kernel[1]
    out_channel = filter_channel * channel_multiplier
    out_height = (in_height + 2 * padding[0] - k_height) // strides[0] + 1
    out_width = (in_width + 2 * padding[1] - k_width) // strides[1] + 1
    tile_ic, tile_oc = cfg["tile_ic"].size[-1], cfg["tile_oc"].size[-1]
    in_shape = (batch_size, in_channel // tile_ic, in_height, in_width, tile_ic)
    in_layout = "NCHW%dc" % tile_ic
    out_shape = (batch_size, out_channel // tile_oc, out_height, out_width, tile_oc)
    out_layout = "NCHW%dc" % tile_oc
    return ((in_shape, in_layout),), ((out_shape, out_layout),)

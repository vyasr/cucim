import functools

import cupy as cp

import cucim.skimage._vendored.ndimage as ndi

from .._shared import utils
from .._shared.filters import gaussian
from .._shared.utils import _supported_float_type, check_shape_equality, warn
from ..util.arraycrop import crop
from ..util.dtype import dtype_range

__all__ = ['structural_similarity']


_ssim_operation = """
    F vx, vy, vxy, C1, C2, A1, A2, B1, B2, D;
    vx = cov_norm * (uxx - ux * ux);
    vy = cov_norm * (uyy - uy * uy);
    vxy = cov_norm * (uxy - ux * uy);

    C1 = (K1 * data_range);
    C1 *= C1;
    C2 = (K2 * data_range);
    C2 *= C2;

    A1 = 2 * ux * uy + C1;
    A2 = 2 * vxy + C2;
    B1 = ux * ux + uy * uy + C1;
    B2 = vx + vy + C2;
    D = B1 * B2;
    ssim = (A1 * A2) / D;
"""


@cp.memoize(for_each_device=True)
def _get_ssim_kernel():
    return cp.ElementwiseKernel(
        in_params='float64 cov_norm, F ux, F uy, F uxx, F uyy, F uxy, float64 data_range, float64 K1, float64 K2',  # noqa
        out_params='F ssim',
        operation=_ssim_operation,
        name='cucim_ssim'
    )


@cp.memoize(for_each_device=True)
def _get_ssim_grad_kernel():
    return cp.ElementwiseKernel(
        in_params='float64 cov_norm, F ux, F uy, F uxx, F uyy, F uxy, float64 data_range, float64 K1, float64 K2',  # noqa
        out_params='F ssim, F grad_temp1, F grad_temp2, F grad_temp3',
        operation=_ssim_operation + """
            grad_temp1 = A1 / D;
            grad_temp2 = -ssim / B2;
            grad_temp3 = (ux * (A2 - A1) - uy * (B2 - B1) * ssim) / D;
        """,
        name='cucim_ssim'
    )


def structural_similarity(im1, im2,
                          *,
                          win_size=None, gradient=False, data_range=None,
                          channel_axis=None, gaussian_weights=False,
                          full=False, **kwargs):
    """
    Compute the mean structural similarity index between two images.
    Please pay attention to the `data_range` parameter with floating-point
    images.

    Parameters
    ----------
    im1, im2 : ndarray
        Images. Any dimensionality with same shape.
    win_size : int or None, optional
        The side-length of the sliding window used in comparison. Must be an
        odd value. If `gaussian_weights` is True, this is ignored and the
        window size will depend on `sigma`.
    gradient : bool, optional
        If True, also return the gradient with respect to im2.
    data_range : float, optional
        The data range of the input image (distance between minimum and
        maximum possible values). By default, this is estimated from the image
        data type. This estimate may be wrong for floating-point image data.
        Therefore it is recommended to always pass this value explicitly
        (see note below).
    channel_axis : int or None, optional
        If None, the image is assumed to be a grayscale (single channel) image.
        Otherwise, this parameter indicates which axis of the array corresponds
        to channels.
    gaussian_weights : bool, optional
        If True, each patch has its mean and variance spatially weighted by a
        normalized Gaussian kernel of width sigma=1.5.
    full : bool, optional
        If True, also return the full structural similarity image.

    Other Parameters
    ----------------
    use_sample_covariance : bool
        If True, normalize covariances by N-1 rather than, N where N is the
        number of pixels within the sliding window.
    K1 : float
        Algorithm parameter, K1 (small constant, see [1]_).
    K2 : float
        Algorithm parameter, K2 (small constant, see [1]_).
    sigma : float
        Standard deviation for the Gaussian when `gaussian_weights` is True.

    Returns
    -------
    mssim : float
        The mean structural similarity index over the image.
    grad : ndarray
        The gradient of the structural similarity between im1 and im2 [2]_.
        This is only returned if `gradient` is set to True.
    S : ndarray
        The full SSIM image.  This is only returned if `full` is set to True.

    Notes
    -----
    If `data_range` is not specified, the range is automatically guessed
    based on the image data type. However for floating-point image data, this
    estimate yields a result double the value of the desired range, as the
    `dtype_range` in `skimage.util.dtype.py` has defined intervals from -1 to
    +1. This yields an estimate of 2, instead of 1, which is most often
    required when working with image data (as negative light intentsities are
    nonsensical). In case of working with YCbCr-like color data, note that
    these ranges are different per channel (Cb and Cr have double the range
    of Y), so one cannot calculate a channel-averaged SSIM with a single call
    to this function, as identical ranges are assumed for each channel.

    To match the implementation of Wang et al. [1]_, set `gaussian_weights`
    to True, `sigma` to 1.5, `use_sample_covariance` to False, and
    specify the `data_range` argument.

    .. versionchanged:: 0.16
        This function was renamed from ``skimage.measure.compare_ssim`` to
        ``skimage.metrics.structural_similarity``.

    References
    ----------
    .. [1] Wang, Z., Bovik, A. C., Sheikh, H. R., & Simoncelli, E. P.
       (2004). Image quality assessment: From error visibility to
       structural similarity. IEEE Transactions on Image Processing,
       13, 600-612.
       https://ece.uwaterloo.ca/~z70wang/publications/ssim.pdf,
       :DOI:`10.1109/TIP.2003.819861`

    .. [2] Avanaki, A. N. (2009). Exact global histogram specification
       optimized for structural similarity. Optical Review, 16, 613-621.
       :arxiv:`0901.0065`
       :DOI:`10.1007/s10043-009-0119-z`

    """
    check_shape_equality(im1, im2)
    float_type = _supported_float_type(im1.dtype)

    if isinstance(data_range, cp.ndarray):
        if data_range.ndim != 0:
            raise ValueError("data_range must be a scalar")
        # need a host scalar
        data_range = float(data_range)

    if channel_axis is not None:
        # loop over channels
        args = dict(win_size=win_size,
                    gradient=gradient,
                    data_range=data_range,
                    channel_axis=None,
                    gaussian_weights=gaussian_weights,
                    full=full)
        args.update(kwargs)
        nch = im1.shape[channel_axis]
        mssim = cp.empty(nch, dtype=float_type)
        if gradient:
            G = cp.empty(im1.shape, dtype=float_type)
        if full:
            S = cp.empty(im1.shape, dtype=float_type)
        channel_axis = channel_axis % im1.ndim
        _at = functools.partial(utils.slice_at_axis, axis=channel_axis)
        for ch in range(nch):
            ch_result = structural_similarity(im1[_at(ch)],
                                              im2[_at(ch)], **args)
            if gradient and full:
                mssim[ch], G[_at(ch)], S[_at(ch)] = ch_result
            elif gradient:
                mssim[ch], G[_at(ch)] = ch_result
            elif full:
                mssim[ch], S[_at(ch)] = ch_result
            else:
                mssim[ch] = ch_result
        mssim = mssim.mean()
        if gradient and full:
            return mssim, G, S
        elif gradient:
            return mssim, G
        elif full:
            return mssim, S
        else:
            return mssim

    K1 = kwargs.pop('K1', 0.01)
    K2 = kwargs.pop('K2', 0.03)
    sigma = kwargs.pop('sigma', 1.5)
    if K1 < 0:
        raise ValueError("K1 must be positive")
    if K2 < 0:
        raise ValueError("K2 must be positive")
    if sigma < 0:
        raise ValueError("sigma must be positive")
    use_sample_covariance = kwargs.pop('use_sample_covariance', True)

    if gaussian_weights:
        # Set to give an 11-tap filter with the default sigma of 1.5 to match
        # Wang et. al. 2004.
        truncate = 3.5

    if win_size is None:
        if gaussian_weights:
            # set win_size used by crop to match the filter size
            r = int(truncate * sigma + 0.5)  # radius as in ndimage
            win_size = 2 * r + 1
        else:
            win_size = 7  # backwards compatibility

    if any(s < win_size for s in im1.shape):
        raise ValueError(
            'win_size exceeds image extent. '
            'Either ensure that your images are '
            'at least 7x7; or pass win_size explicitly '
            'in the function call, with an odd value '
            'less than or equal to the smaller side of your '
            'images. If your images are multichannel '
            '(with color channels), set channel_axis to '
            'the axis number corresponding to the channels.')

    if not (win_size % 2 == 1):
        raise ValueError('Window size must be odd.')

    if data_range is None:
        if (
            cp.issubdtype(im1.dtype, cp.floating) or
            cp.issubdtype(im2.dtype, cp.floating)
        ):
            raise ValueError(
                'Since image dtype is floating point, you must specify '
                'the data_range parameter. Please read the documentation '
                'carefully (including the note). It is recommended that '
                'you always specify the data_range anyway.')
        if im1.dtype != im2.dtype:
            warn("Inputs have mismatched dtypes.  Setting data_range based on "
                 "im1.dtype.", stacklevel=2)
        dmin, dmax = dtype_range[im1.dtype.type]
        data_range = float(dmax - dmin)
        if cp.issubdtype(im1.dtype, cp.integer) and (im1.dtype != cp.uint8):
            warn("Setting data_range based on im1.dtype. " +
                 ("data_range = %.0f. " % data_range) +
                 "Please specify data_range explicitly to avoid mistakes.",
                 stacklevel=2)

    ndim = im1.ndim

    if gaussian_weights:
        filter_func = gaussian
        filter_args = {'sigma': sigma, 'truncate': truncate, 'mode': 'reflect'}
    else:
        filter_func = ndi.uniform_filter
        filter_args = {'size': win_size}

    # ndimage filters need floating point data
    im1 = im1.astype(float_type, copy=False)
    im2 = im2.astype(float_type, copy=False)

    NP = win_size ** ndim

    # filter has already normalized by NP
    if use_sample_covariance:
        cov_norm = NP / (NP - 1)  # sample covariance
    else:
        cov_norm = 1.0  # population covariance to match Wang et. al. 2004

    # compute (weighted) means
    ux = filter_func(im1, **filter_args)
    uy = filter_func(im2, **filter_args)

    # compute (weighted) variances and covariances
    uxx = filter_func(im1 * im1, **filter_args)
    uyy = filter_func(im2 * im2, **filter_args)
    uxy = filter_func(im1 * im2, **filter_args)

    if not gradient:
        S = cp.empty_like(ux)
        kernel = _get_ssim_kernel()
        kernel(cov_norm, ux, uy, uxx, uyy, uxy, data_range, K1, K2, S)
    else:
        S = cp.empty_like(ux)
        grad_temp1 = cp.empty_like(ux)
        grad_temp2 = cp.empty_like(ux)
        grad_temp3 = cp.empty_like(ux)
        kernel = _get_ssim_grad_kernel()
        kernel(cov_norm, ux, uy, uxx, uyy, uxy, data_range, K1, K2, S,
               grad_temp1, grad_temp2, grad_temp3)

    # to avoid edge effects will ignore filter radius strip around edges
    pad = (win_size - 1) // 2

    # compute (weighted) mean of ssim. Use float64 for accuracy.
    mssim = crop(S, pad).mean(dtype=cp.float64)

    if gradient:
        # The following is Eqs. 7-8 of Avanaki 2009.
        grad = filter_func(grad_temp1, **filter_args) * im1
        grad += filter_func(grad_temp2, **filter_args) * im2
        grad += filter_func(grad_temp3, **filter_args)
        grad *= (2 / im1.size)

        if full:
            return mssim, grad, S
        else:
            return mssim, grad
    else:
        if full:
            return mssim, S
        else:
            return mssim

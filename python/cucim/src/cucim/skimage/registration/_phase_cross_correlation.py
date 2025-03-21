"""
Port of Manuel Guizar's code from:
http://www.mathworks.com/matlabcentral/fileexchange/18401-efficient-subpixel-image-registration-by-cross-correlation
"""

import itertools
import math
import warnings

import cupy as cp
import cupyx.scipy.ndimage as ndi
import numpy as np

from .._shared.fft import fftmodule as fft
from ._masked_phase_cross_correlation import _masked_phase_cross_correlation


def _upsampled_dft(data, upsampled_region_size,
                   upsample_factor=1, axis_offsets=None):
    """
    Upsampled DFT by matrix multiplication.

    This code is intended to provide the same result as if the following
    operations were performed:
        - Embed the array "data" in an array that is ``upsample_factor`` times
          larger in each dimension.  ifftshift to bring the center of the
          image to (1,1).
        - Take the FFT of the larger array.
        - Extract an ``[upsampled_region_size]`` region of the result, starting
          with the ``[axis_offsets+1]`` element.

    It achieves this result by computing the DFT in the output array without
    the need to zeropad. Much faster and memory efficient than the zero-padded
    FFT approach if ``upsampled_region_size`` is much smaller than
    ``data.size * upsample_factor``.

    Parameters
    ----------
    data : array
        The input data array (DFT of original data) to upsample.
    upsampled_region_size : integer or tuple of integers, optional
        The size of the region to be sampled.  If one integer is provided, it
        is duplicated up to the dimensionality of ``data``.
    upsample_factor : integer, optional
        The upsampling factor.  Defaults to 1.
    axis_offsets : tuple of integers, optional
        The offsets of the region to be sampled.  Defaults to None (uses
        image center)

    Returns
    -------
    output : ndarray
            The upsampled DFT of the specified region.
    """
    # if people pass in an integer, expand it to a list of equal-sized sections
    if not hasattr(upsampled_region_size, "__iter__"):
        upsampled_region_size = [upsampled_region_size] * data.ndim
    else:
        if len(upsampled_region_size) != data.ndim:
            raise ValueError("shape of upsampled region sizes must be equal "
                             "to input data's number of dimensions.")

    if axis_offsets is None:
        axis_offsets = [0] * data.ndim
    else:
        if len(axis_offsets) != data.ndim:
            raise ValueError("number of axis offsets must be equal to input "
                             "data's number of dimensions.")

    im2pi = 1j * 2 * np.pi

    dim_properties = list(zip(data.shape, upsampled_region_size, axis_offsets))

    for (n_items, ups_size, ax_offset) in dim_properties[::-1]:
        kernel = ((cp.arange(ups_size) - ax_offset)[:, None]
                  * fft.fftfreq(n_items, upsample_factor))
        kernel = cp.exp(-im2pi * kernel)
        # CuPy Backend: use kernel of same precision as the data
        kernel = kernel.astype(data.dtype, copy=False)

        # Equivalent to:
        #   data[i, j, k] = kernel[i, :] @ data[j, k].T
        data = cp.tensordot(kernel, data, axes=(1, -1))
    return data


def _compute_phasediff(cross_correlation_max):
    """
    Compute global phase difference between the two images (should be
        zero if images are non-negative).

    Parameters
    ----------
    cross_correlation_max : complex
        The complex value of the cross correlation at its maximum point.
    """
    return cp.arctan2(cross_correlation_max.imag, cross_correlation_max.real)


def _compute_error(cross_correlation_max, src_amp, target_amp):
    """
    Compute RMS error metric between ``src_image`` and ``target_image``.

    Parameters
    ----------
    cross_correlation_max : complex
        The complex value of the cross correlation at its maximum point.
    src_amp : float
        The normalized average image intensity of the source image
    target_amp : float
        The normalized average image intensity of the target image
    """
    error = 1.0 - cross_correlation_max * cross_correlation_max.conj() /\
        (src_amp * target_amp)

    return cp.sqrt(cp.abs(error))


def _disambiguate_shift(reference_image, moving_image, shift):
    """Determine the correct real-space shift based on periodic shift.

    When determining a translation shift from phase cross-correlation in
    Fourier space, the shift is only correct to within a period of the image
    size along each axis, resulting in $2^n$ possible shifts, where $n$ is the
    number of dimensions of the image. This function checks the
    cross-correlation in real space for each of those shifts, and returns the
    one with the highest cross-correlation.

    The strategy we use is to perform the shift on the moving image *using the
    'grid-wrap' mode* in `scipy.ndimage`. The moving image's original borders
    then define $2^n$ quadrants, which we cross-correlate with the reference
    image in turn using slicing. The entire operation is thus $O(2^n + m)$,
    where $m$ is the number of pixels in the image (and typically dominates).

    Parameters
    ----------
    reference_image : numpy array
        The reference (non-moving) image.
    moving_image : numpy array
        The moving image: applying the shift to this image overlays it on the
        reference image. Must be the same shape as the reference image.
    shift : tuple of float
        The shift to apply to each axis of the moving image, *modulo* image
        size. The length of ``shift`` must be equal to ``moving_image.ndim``.

    Returns
    -------
    real_shift : tuple of float
        The shift disambiguated in real space.
    """
    shape = reference_image.shape
    positive_shift = [shift_i % s for shift_i, s in zip(shift, shape)]
    negative_shift = [shift_i - s
                      for shift_i, s in zip(positive_shift, shape)]
    subpixel = any(s % 1 != 0 for s in shift)
    interp_order = 3 if subpixel else 0
    shifted = ndi.shift(
        moving_image, shift, mode='grid-wrap', order=interp_order
    )
    indices = tuple(round(s) for s in positive_shift)
    splits_per_dim = [(slice(0, i), slice(i, None)) for i in indices]
    max_corr = -1.0
    max_slice = None
    for test_slice in itertools.product(*splits_per_dim):
        reference_tile = cp.reshape(reference_image[test_slice], -1)
        moving_tile = cp.reshape(shifted[test_slice], -1)
        corr = -1.0
        if reference_tile.size > 2:
            corr = float(cp.corrcoef(reference_tile, moving_tile)[0, 1])
        if corr > max_corr:
            max_corr = corr
            max_slice = test_slice
    real_shift_acc = []
    for sl, pos_shift, neg_shift in zip(
        max_slice, positive_shift, negative_shift
    ):
        real_shift_acc.append(pos_shift if sl.stop is None else neg_shift)
    if not subpixel:
        real_shift = tuple(map(int, real_shift_acc))
    else:
        real_shift = tuple(real_shift_acc)
    return real_shift


def phase_cross_correlation(reference_image, moving_image, *,
                            upsample_factor=1, space="real",
                            disambiguate=False,
                            return_error=True, reference_mask=None,
                            moving_mask=None, overlap_ratio=0.3,
                            normalization="phase"):
    """Efficient subpixel image translation registration by cross-correlation.

    This code gives the same precision as the FFT upsampled cross-correlation
    in a fraction of the computation time and with reduced memory requirements.
    It obtains an initial estimate of the cross-correlation peak by an FFT and
    then refines the shift estimation by upsampling the DFT only in a small
    neighborhood of that estimate by means of a matrix-multiply DFT [1]_.

    Parameters
    ----------
    reference_image : array
        Reference image.
    moving_image : array
        Image to register. Must be same dimensionality as
        ``reference_image``.
    upsample_factor : int, optional
        Upsampling factor. Images will be registered to within
        ``1 / upsample_factor`` of a pixel. For example
        ``upsample_factor == 20`` means the images will be registered
        within 1/20th of a pixel. Default is 1 (no upsampling).
        Not used if any of ``reference_mask`` or ``moving_mask`` is not None.
    space : string, one of "real" or "fourier", optional
        Defines how the algorithm interprets input data. "real" means
        data will be FFT'd to compute the correlation, while "fourier"
        data will bypass FFT of input data. Case insensitive. Not
        used if any of ``reference_mask`` or ``moving_mask`` is not
        None.
    disambiguate : bool
        The shift returned by this function is only accurate *modulo* the
        image shape, due to the periodic nature of the Fourier transform. If
        this parameter is set to ``True``, the *real* space cross-correlation
        is computed for each possible shift, and the shift with the highest
        cross-correlation within the overlapping area is returned.
    return_error : bool, {"always"}, optional
        Returns error and phase difference if "always" is given. If False, or
        either ``reference_mask`` or ``moving_mask`` are given, only the shift
        is returned.
    reference_mask : ndarray
        Boolean mask for ``reference_image``. The mask should evaluate
        to ``True`` (or 1) on valid pixels. ``reference_mask`` should
        have the same shape as ``reference_image``.
    moving_mask : ndarray or None, optional
        Boolean mask for ``moving_image``. The mask should evaluate to ``True``
        (or 1) on valid pixels. ``moving_mask`` should have the same shape
        as ``moving_image``. If ``None``, ``reference_mask`` will be used.
    overlap_ratio : float, optional
        Minimum allowed overlap ratio between images. The correlation for
        translations corresponding with an overlap ratio lower than this
        threshold will be ignored. A lower `overlap_ratio` leads to smaller
        maximum translation, while a higher `overlap_ratio` leads to greater
        robustness against spurious matches due to small overlap between
        masked images. Used only if one of ``reference_mask`` or
        ``moving_mask`` is None.
    normalization : {"phase", None}
        The type of normalization to apply to the cross-correlation. This
        parameter is unused when masks (`reference_mask` and `moving_mask`) are
        supplied.

    Returns
    -------
    shift : tuple
        Shift vector (in pixels) required to register ``moving_image``
        with ``reference_image``. Axis ordering is consistent with
        the axis order of the input array.
    error : float
        Translation invariant normalized RMS error between
        ``reference_image`` and ``moving_image``. For masked cross-correlation
        this error is not available and NaN is returned if ``return_error``
        is "always".
    phasediff : float
        Global phase difference between the two images (should be
        zero if images are non-negative). For masked cross-correlation
        this phase difference is not available and NaN is returned if
        ``return_error`` is "always".

    Notes
    -----
    The use of cross-correlation to estimate image translation has a long
    history dating back to at least [2]_. The "phase correlation"
    method (selected by ``normalization="phase"``) was first proposed in [3]_.
    Publications [1]_ and [2]_ use an unnormalized cross-correlation
    (``normalization=None``). Which form of normalization is better is
    application-dependent. For example, the phase correlation method works
    well in registering images under different illumination, but is not very
    robust to noise. In a high noise scenario, the unnormalized method may be
    preferable.

    When masks are provided, a masked normalized cross-correlation algorithm is
    used [5]_, [6]_.

    References
    ----------
    .. [1] Manuel Guizar-Sicairos, Samuel T. Thurman, and James R. Fienup,
           "Efficient subpixel image registration algorithms,"
           Optics Letters 33, 156-158 (2008). :DOI:`10.1364/OL.33.000156`
    .. [2] P. Anuta, Spatial registration of multispectral and multitemporal
           digital imagery using fast Fourier transform techniques, IEEE Trans.
           Geosci. Electron., vol. 8, no. 4, pp. 353–368, Oct. 1970.
           :DOI:`10.1109/TGE.1970.271435`.
    .. [3] C. D. Kuglin D. C. Hines. The phase correlation image alignment
           method, Proceeding of IEEE International Conference on Cybernetics
           and Society, pp. 163-165, New York, NY, USA, 1975, pp. 163–165.
    .. [4] James R. Fienup, "Invariant error metrics for image reconstruction"
           Optics Letters 36, 8352-8357 (1997). :DOI:`10.1364/AO.36.008352`
    .. [5] Dirk Padfield. Masked Object Registration in the Fourier Domain.
           IEEE Transactions on Image Processing, vol. 21(5),
           pp. 2706-2718 (2012). :DOI:`10.1109/TIP.2011.2181402`
    .. [6] D. Padfield. "Masked FFT registration". In Proc. Computer Vision and
           Pattern Recognition, pp. 2918-2925 (2010).
           :DOI:`10.1109/CVPR.2010.5540032`
    """
    def warn_return_error():
        warnings.warn(
            "In scikit-image 0.22, phase_cross_correlation will start "
            "returning a tuple or 3 items (shift, error, phasediff) always. "
            "To enable the new return behavior and silence this warning, use "
            "return_error='always'.",
            category=FutureWarning,
            stacklevel=3,
        )

    if (reference_mask is not None) or (moving_mask is not None):
        shift = _masked_phase_cross_correlation(reference_image, moving_image,
                                                reference_mask, moving_mask,
                                                overlap_ratio)
        if return_error == "always":
            return shift, np.nan, np.nan
        else:
            warn_return_error()
            return shift

    # images must be the same shape
    if reference_image.shape != moving_image.shape:
        raise ValueError("images must be same shape")

    # assume complex data is already in Fourier space
    if space.lower() == 'fourier':
        src_freq = reference_image
        target_freq = moving_image
    # real data needs to be fft'd.
    elif space.lower() == 'real':
        src_freq = fft.fftn(reference_image)
        target_freq = fft.fftn(moving_image)
    else:
        raise ValueError('space argument must be "real" of "fourier"')

    # Whole-pixel shift - Compute cross-correlation by an IFFT
    shape = src_freq.shape
    image_product = src_freq * target_freq.conj()
    if normalization == "phase":
        eps = cp.finfo(image_product.real.dtype).eps
        image_product /= cp.maximum(cp.abs(image_product), 100 * eps)
    elif normalization is not None:
        raise ValueError("normalization must be either phase or None")
    cross_correlation = fft.ifftn(image_product)

    # Locate maximum
    maxima = np.unravel_index(
        int(cp.argmax(cp.abs(cross_correlation))), cross_correlation.shape
    )
    midpoint = tuple(float(axis_size // 2) for axis_size in shape)
    shift = tuple(_max - axis_size if _max > mid else _max
                  for _max, mid, axis_size in zip(maxima, midpoint, shape))

    if upsample_factor == 1:
        if return_error:
            sabs = cp.abs(src_freq)
            sabs *= sabs
            tabs = cp.abs(target_freq)
            tabs *= tabs
            src_amp = np.sum(sabs) / src_freq.size
            target_amp = np.sum(tabs) / target_freq.size
            CCmax = cross_correlation[maxima]
    # If upsampling > 1, then refine estimate with matrix multiply DFT
    else:
        # Initial shift estimate in upsampled grid
        # shift = cp.around(shift * upsample_factor) / upsample_factor
        upsample_factor = float(upsample_factor)
        shift = tuple(round(s * upsample_factor) / upsample_factor
                      for s in shift)
        upsampled_region_size = math.ceil(upsample_factor * 1.5)
        # Center of output array at dftshift + 1
        dftshift = float(upsampled_region_size // 2)
        # Matrix multiply DFT around the current shift estimate
        sample_region_offset = tuple(
            dftshift - s * upsample_factor for s in shift
        )
        cross_correlation = _upsampled_dft(image_product.conj(),
                                           upsampled_region_size,
                                           upsample_factor,
                                           sample_region_offset).conj()

        # Locate maximum and map back to original pixel grid
        maxima = np.unravel_index(int(cp.argmax(cp.abs(cross_correlation))),
                                  cross_correlation.shape)
        CCmax = cross_correlation[maxima]

        maxima = tuple(float(m) - dftshift for m in maxima)
        shift = tuple(s + m / upsample_factor for s, m in zip(shift, maxima))

        if return_error:
            src_amp = cp.abs(src_freq)
            src_amp *= src_amp
            src_amp = cp.sum(src_amp)
            target_amp = cp.abs(target_freq)
            target_amp *= target_amp
            target_amp = cp.sum(target_amp)

    # If its only one row or column the shift along that dimension has no
    # effect. We set to zero.
    shift = tuple(
        s if axis_size != 1 else 0 for s, axis_size in zip(shift, shape)
    )

    if disambiguate:
        if space.lower() != 'real':
            reference_image = fft.ifftn(reference_image)
            moving_image = fft.ifftn(moving_image)
        shift = _disambiguate_shift(reference_image, moving_image, shift)

    if return_error:
        # Redirect user to masked_phase_cross_correlation if NaNs are observed
        if cp.isnan(CCmax) or cp.isnan(src_amp) or cp.isnan(target_amp):
            raise ValueError(
                "NaN values found, please remove NaNs from your "
                "input data or use the `reference_mask`/`moving_mask` "
                "keywords, eg: "
                "phase_cross_correlation(reference_image, moving_image, "
                "reference_mask=~np.isnan(reference_image), "
                "moving_mask=~np.isnan(moving_image))")

        return shift, _compute_error(CCmax, src_amp, target_amp),\
            _compute_phasediff(CCmax)
    else:
        return shift

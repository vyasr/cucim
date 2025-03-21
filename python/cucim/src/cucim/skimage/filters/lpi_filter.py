"""
:author: Stefan van der Walt, 2008
:license: modified BSD
"""

import cupy as cp
import numpy as np
from cupyx.scipy import fft

from .._shared.utils import _supported_float_type, check_nD, deprecated

eps = np.finfo(float).eps


def _min_limit(x, val=eps):
    mask = cp.abs(x) < eps
    x[mask] = cp.sign(x[mask]) * eps


def _center(x, oshape):
    """Return an array of shape ``oshape`` from the center of array ``x``."""
    start = (np.array(x.shape) - np.array(oshape)) // 2 + 1
    out = x[tuple(slice(s, s + n) for s, n in zip(start, oshape))]
    return out


def _pad(data, shape):
    """Pad the data to the given shape with zeros.

    Parameters
    ----------
    data : 2-d ndarray
        Input data
    shape : (2,) tuple

    """
    out = cp.zeros(shape, dtype=data.dtype)
    out[tuple(slice(0, n) for n in data.shape)] = data
    return out


class LPIFilter2D:
    """Linear Position-Invariant Filter (2-dimensional)"""

    def __init__(self, impulse_response, **filter_params):
        """
        Parameters
        ----------
        impulse_response : callable `f(r, c, **filter_params)`
            Function that yields the impulse response.  ``r`` and ``c`` are
            1-dimensional vectors that represent row and column positions, in
            other words coordinates are (r[0],c[0]),(r[0],c[1]) etc.
            `**filter_params` are passed through.

            In other words, ``impulse_response`` would be called like this:

            >>> def impulse_response(r, c, **filter_params):
            ...     pass
            >>>
            >>> r = [0,0,0,1,1,1,2,2,2]
            >>> c = [0,1,2,0,1,2,0,1,2]
            >>> filter_params = {'kw1': 1, 'kw2': 2, 'kw3': 3}
            >>> impulse_response(r, c, **filter_params)


        Examples
        --------
        Gaussian filter: Use a 1-D gaussian in each direction without
        normalization coefficients.

        >>> def filt_func(r, c, sigma = 1):
        ...     return cp.exp(-cp.hypot(r, c)/sigma)
        >>> filter = LPIFilter2D(filt_func)

        """
        if not callable(impulse_response):
            raise ValueError("Impulse response must be a callable.")

        self.impulse_response = impulse_response
        self.filter_params = filter_params
        self._cache = None

    def _prepare(self, data):
        """Calculate filter and data FFT in preparation for filtering."""
        dshape = np.array(data.shape)
        dshape += dshape % 2 == 0  # all filter dimensions must be uneven
        oshape = np.array(data.shape) * 2 - 1

        float_dtype = _supported_float_type(data.dtype)
        data = data.astype(float_dtype, copy=False)

        if self._cache is None or np.any(self._cache.shape != oshape):
            coords = cp.mgrid[[slice(0, float(n)) for n in dshape]]
            # this steps over two sets of coordinates,
            # not over the coordinates individually
            for k, coord in enumerate(coords):
                coord -= (dshape[k] - 1) / 2.0
            coords = coords.reshape(2, -1).T  # coordinate pairs (r,c)
            coords = coords.astype(float_dtype, copy=False)

            f = self.impulse_response(coords[:, 0], coords[:, 1],
                                      **self.filter_params).reshape(dshape)

            f = _pad(f, oshape)
            F = fft.fftn(f)
            self._cache = F
        else:
            F = self._cache

        data = _pad(data, oshape)
        G = fft.fftn(data)

        return F, G

    def __call__(self, data):
        """Apply the filter to the given data.

        Parameters
        ----------
        data : (M, N) ndarray

        """
        check_nD(data, 2, 'data')
        F, G = self._prepare(data)
        out = fft.ifftn(F * G)
        out = cp.abs(_center(out, data.shape))
        return out


def filter_forward(data, impulse_response=None, filter_params={},
                   predefined_filter=None):
    """Apply the given filter to data.

    Parameters
    ----------
    data : (M, N) ndarray
        Input data.
    impulse_response : callable `f(r, c, **filter_params)`
        Impulse response of the filter.  See LPIFilter2D.__init__.
    filter_params : dict
        Additional keyword parameters to the impulse_response function.

    Other Parameters
    ----------------
    predefined_filter : LPIFilter2D
        If you need to apply the same filter multiple times over different
        images, construct the LPIFilter2D and specify it here.

    Examples
    --------

    Gaussian filter:

    >>> import cupy as cp
    >>> def filt_func(r, c):
    ...     return cp.exp(-cp.hypot(r, c)/1)
    >>>
    >>> from skimage import data
    >>> filtered = filter_forward(cp.array(data.coins()), filt_func)

    """
    check_nD(data, 2, 'data')
    if predefined_filter is None:
        predefined_filter = LPIFilter2D(impulse_response, **filter_params)
    return predefined_filter(data)


@deprecated(alt_func='cucim.skimage.filters.lpi_filter.filter_inverse',
            removed_version='2023.06.00')
def inverse(data, impulse_response=None, filter_params={}, max_gain=2,
            predefined_filter=None):
    return filter_inverse(data, impulse_response, filter_params,
                          max_gain, predefined_filter)


def filter_inverse(data, impulse_response=None, filter_params={}, max_gain=2,
                   predefined_filter=None):
    """Apply the filter in reverse to the given data.

    Parameters
    ----------
    data : (M, N) ndarray
        Input data.
    impulse_response : callable `f(r, c, **filter_params)`
        Impulse response of the filter.  See LPIFilter2D.__init__.
    filter_params : dict
        Additional keyword parameters to the impulse_response function.
    max_gain : float
        Limit the filter gain.  Often, the filter contains zeros, which would
        cause the inverse filter to have infinite gain.  High gain causes
        amplification of artefacts, so a conservative limit is recommended.

    Other Parameters
    ----------------
    predefined_filter : LPIFilter2D
        If you need to apply the same filter multiple times over different
        images, construct the LPIFilter2D and specify it here.

    """
    check_nD(data, 2, 'data')
    if predefined_filter is None:
        filt = LPIFilter2D(impulse_response, **filter_params)
    else:
        filt = predefined_filter

    F, G = filt._prepare(data)
    _min_limit(F)

    F = 1 / F
    mask = cp.abs(F) > max_gain
    F[mask] = cp.sign(F[mask]) * max_gain

    return _center(cp.abs(fft.ifftshift(fft.ifftn(G * F))), data.shape)


def wiener(data, impulse_response=None, filter_params={}, K=0.25,
           predefined_filter=None):
    """Minimum Mean Square Error (Wiener) inverse filter.

    Parameters
    ----------
    data : (M,N) ndarray
        Input data.
    K : float or (M,N) ndarray
        Ratio between power spectrum of noise and undegraded
        image.
    impulse_response : callable `f(r, c, **filter_params)`
        Impulse response of the filter.  See LPIFilter2D.__init__.
    filter_params : dict
        Additional keyword parameters to the impulse_response function.

    Other Parameters
    ----------------
    predefined_filter : LPIFilter2D
        If you need to apply the same filter multiple times over different
        images, construct the LPIFilter2D and specify it here.

    """
    check_nD(data, 2, 'data')

    if not isinstance(K, float):
        check_nD(K, 2, 'K')

    if predefined_filter is None:
        filt = LPIFilter2D(impulse_response, **filter_params)
    else:
        filt = predefined_filter

    F, G = filt._prepare(data)
    _min_limit(F)

    H_mag_sqr = cp.abs(F)
    H_mag_sqr *= H_mag_sqr
    F = 1 / F * H_mag_sqr / (H_mag_sqr + K)

    tmp = fft.ifftn(G * F)
    tmp = fft.ifftshift(tmp)
    return _center(cp.abs(tmp), data.shape)

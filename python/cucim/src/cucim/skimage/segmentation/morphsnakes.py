import functools
from itertools import cycle

import cupy as cp
import numpy as np
from cupyx import rsqrt

import cucim.skimage._vendored.ndimage as ndi
from cucim import _misc

from .._shared._gradient import gradient
from .._shared.utils import check_nD, deprecate_kwarg

__all__ = ['morphological_chan_vese',
           'morphological_geodesic_active_contour',
           'inverse_gaussian_gradient',
           'disk_level_set',
           'checkerboard_level_set'
           ]


class _fcycle:

    def __init__(self, iterable):
        """Call functions from the iterable each time it is called."""
        self.funcs = cycle(iterable)

    def __call__(self, *args, **kwargs):
        f = next(self.funcs)
        return f(*args, **kwargs)


# SI and IS operators for 2D and 3D.
def _get_P2():
    _P2 = [cp.eye(3),
           cp.array([[0, 1, 0]] * 3),
           cp.array(np.flipud(np.eye(3))),
           cp.array(np.rot90([[0, 1, 0]] * 3))]
    return _P2


def _get_P3():
    _P3 = [np.zeros((3, 3, 3)) for i in range(9)]

    _P3[0][:, :, 1] = 1
    _P3[1][:, 1, :] = 1
    _P3[2][1, :, :] = 1
    _P3[3][:, [0, 1, 2], [0, 1, 2]] = 1
    _P3[4][:, [0, 1, 2], [2, 1, 0]] = 1
    _P3[5][[0, 1, 2], :, [0, 1, 2]] = 1
    _P3[6][[0, 1, 2], :, [2, 1, 0]] = 1
    _P3[7][[0, 1, 2], [0, 1, 2], :] = 1
    _P3[8][[0, 1, 2], [2, 1, 0], :] = 1
    return [cp.array(p) for p in _P3]


def sup_inf(u, footprints, workspace=None):
    """SI operator."""
    if workspace is None:
        erosions = cp.empty(((len(footprints),) + u.shape), dtype=u.dtype)
    else:
        erosions = workspace
    for i, footprint in enumerate(footprints):
        erosions[i, ...] = ndi.binary_erosion(u, footprint)
    return erosions.max(0)


def inf_sup(u, footprints, workspace=None):
    """IS operator."""
    if workspace is None:
        dilations = cp.empty(((len(footprints),) + u.shape), dtype=u.dtype)
    else:
        dilations = workspace
    for i, footprint in enumerate(footprints):
        dilations[i, ...] = ndi.binary_dilation(u, footprint)
    return dilations.min(0)


_curvop = _fcycle([lambda u, f, w: sup_inf(inf_sup(u, f, w), f, w),   # SIoIS
                   lambda u, f, w: inf_sup(sup_inf(u, f, w), f, w)])  # ISoSI


def _check_input(image, init_level_set):
    """Check that shapes of `image` and `init_level_set` match."""
    check_nD(image, [2, 3])

    if len(image.shape) != len(init_level_set.shape):
        raise ValueError("The dimensions of the initial level set do not "
                         "match the dimensions of the image.")


def _init_level_set(init_level_set, image_shape):
    """Auxiliary function for initializing level sets with a string.

    If `init_level_set` is not a string, it is returned as is.
    """
    if isinstance(init_level_set, str):
        if init_level_set == 'checkerboard':
            res = checkerboard_level_set(image_shape)
        elif init_level_set == 'disk':
            res = disk_level_set(image_shape)
        else:
            raise ValueError("`init_level_set` not in "
                             "['checkerboard', 'circle', 'disk']")
    else:
        res = init_level_set
    return res


def disk_level_set(image_shape, *, center=None, radius=None):
    """Create a disk level set with binary values.

    Parameters
    ----------
    image_shape : tuple of positive integers
        Shape of the image
    center : tuple of positive integers, optional
        Coordinates of the center of the disk given in (row, column). If not
        given, it defaults to the center of the image.
    radius : float, optional
        Radius of the disk. If not given, it is set to the 75% of the
        smallest image dimension.

    Returns
    -------
    out : array with shape `image_shape`
        Binary level set of the disk with the given `radius` and `center`.

    See Also
    --------
    checkerboard_level_set
    """

    if center is None:
        center = tuple(i // 2 for i in image_shape)

    if radius is None:
        radius = min(image_shape) * 3.0 / 8.0

    grid = cp.mgrid[[slice(i) for i in image_shape]]
    grid = (grid.T - cp.asarray(center)).T
    grid *= grid
    phi = radius - cp.sqrt(cp.sum(grid, axis=0))
    res = (phi > 0).astype(cp.int8)
    return res


def checkerboard_level_set(image_shape, square_size=5):
    """Create a checkerboard level set with binary values.

    Parameters
    ----------
    image_shape : tuple of positive integers
        Shape of the image.
    square_size : int, optional
        Size of the squares of the checkerboard. It defaults to 5.

    Returns
    -------
    out : array with shape `image_shape`
        Binary level set of the checkerboard.

    See Also
    --------
    disk_level_set
    """

    grid = cp.mgrid[[slice(i) for i in image_shape]]
    grid = grid // square_size

    # Alternate 0/1 for even/odd numbers.
    grid = grid & 1

    # CuPy Backend: use functools.reduce instead of cp.bitwise_xor.reduce
    #     checkerboard = cp.bitwise_xor.reduce(grid, axis=0)
    checkerboard = functools.reduce(cp.bitwise_xor, [g for g in grid])
    res = checkerboard.astype(cp.int8)
    return res


@cp.fuse()
def _fused_inverse_kernel(gradnorm, alpha):
    return rsqrt(1.0 + alpha * gradnorm)


def inverse_gaussian_gradient(image, alpha=100.0, sigma=5.0):
    """Inverse of gradient magnitude.

    Compute the magnitude of the gradients in the image and then inverts the
    result in the range [0, 1]. Flat areas are assigned values close to 1,
    while areas close to borders are assigned values close to 0.

    This function or a similar one defined by the user should be applied over
    the image as a preprocessing step before calling
    `morphological_geodesic_active_contour`.

    Parameters
    ----------
    image : (M, N) or (L, M, N) array
        Grayscale image or volume.
    alpha : float, optional
        Controls the steepness of the inversion. A larger value will make the
        transition between the flat areas and border areas steeper in the
        resulting array.
    sigma : float, optional
        Standard deviation of the Gaussian filter applied over the image.

    Returns
    -------
    gimage : (M, N) or (L, M, N) array
        Preprocessed image (or volume) suitable for
        `morphological_geodesic_active_contour`.
    """
    gradnorm = ndi.gaussian_gradient_magnitude(image, sigma, mode='nearest')
    return _fused_inverse_kernel(gradnorm, alpha)


@cp.fuse()
def _abs_grad_kernel(gx, gy):
    return cp.abs(gx) + cp.abs(gy)


@cp.fuse()
def _fused_variance_kernel(
    image, c1, c2, lam1, lam2, abs_du,
):
    difference_term = image - c1
    difference_term *= difference_term
    difference_term *= lam1
    term2 = image - c2
    term2 *= term2
    term2 *= lam2
    difference_term -= term2

    aux = abs_du * difference_term
    aux_lt0 = aux < 0
    aux_gt0 = aux > 0
    return aux_lt0, aux_gt0


@deprecate_kwarg({'iterations': 'num_iter'},
                 removed_version="23.02.00",
                 deprecated_version="22.02.00")
def morphological_chan_vese(image, num_iter, init_level_set='checkerboard',
                            smoothing=1, lambda1=1, lambda2=1,
                            iter_callback=lambda x: None):
    """Morphological Active Contours without Edges (MorphACWE)

    Active contours without edges implemented with morphological operators. It
    can be used to segment objects in images and volumes without well defined
    borders. It is required that the inside of the object looks different on
    average than the outside (i.e., the inner area of the object should be
    darker or lighter than the outer area on average).

    Parameters
    ----------
    image : (M, N) or (L, M, N) array
        Grayscale image or volume to be segmented.
    num_iter : uint
        Number of num_iter to run
    init_level_set : str, (M, N) array, or (L, M, N) array
        Initial level set. If an array is given, it will be binarized and used
        as the initial level set. If a string is given, it defines the method
        to generate a reasonable initial level set with the shape of the
        `image`. Accepted values are 'checkerboard' and 'disk'. See the
        documentation of `checkerboard_level_set` and `disk_level_set`
        respectively for details about how these level sets are created.
    smoothing : uint, optional
        Number of times the smoothing operator is applied per iteration.
        Reasonable values are around 1-4. Larger values lead to smoother
        segmentations.
    lambda1 : float, optional
        Weight parameter for the outer region. If `lambda1` is larger than
        `lambda2`, the outer region will contain a larger range of values than
        the inner region.
    lambda2 : float, optional
        Weight parameter for the inner region. If `lambda2` is larger than
        `lambda1`, the inner region will contain a larger range of values than
        the outer region.
    iter_callback : function, optional
        If given, this function is called once per iteration with the current
        level set as the only argument. This is useful for debugging or for
        plotting intermediate results during the evolution.

    Returns
    -------
    out : (M, N) or (L, M, N) array
        Final segmentation (i.e., the final level set)

    See Also
    --------
    disk_level_set, checkerboard_level_set

    Notes
    -----
    This is a version of the Chan-Vese algorithm that uses morphological
    operators instead of solving a partial differential equation (PDE) for the
    evolution of the contour. The set of morphological operators used in this
    algorithm are proved to be infinitesimally equivalent to the Chan-Vese PDE
    (see [1]_). However, morphological operators are do not suffer from the
    numerical stability issues typically found in PDEs (it is not necessary to
    find the right time step for the evolution), and are computationally
    faster.

    The algorithm and its theoretical derivation are described in [1]_.

    References
    ----------
    .. [1] A Morphological Approach to Curvature-based Evolution of Curves and
           Surfaces, Pablo Márquez-Neila, Luis Baumela, Luis Álvarez. In IEEE
           Transactions on Pattern Analysis and Machine Intelligence (PAMI),
           2014, :DOI:`10.1109/TPAMI.2013.106`
    """

    init_level_set = _init_level_set(init_level_set, image.shape)

    _check_input(image, init_level_set)

    u = (init_level_set > 0).astype(cp.int8)

    if _misc.ndim(u) == 2:
        footprints = _get_P2()
    elif _misc.ndim(u) == 3:
        footprints = _get_P3()
    else:
        raise ValueError("u has an invalid number of dimensions "
                         "(should be 2 or 3)")
    workspace = cp.empty(((len(footprints),) + u.shape), dtype=u.dtype)

    iter_callback(u)
    for i in range(num_iter):

        # inside = u > 0
        # outside = u <= 0
        c0 = (image * (1 - u)).sum()
        c0 /= float((1 - u).sum() + 1e-8)
        c1 = (image * u).sum()
        c1 /= float(u.sum() + 1e-8)

        # Image attachment
        du = gradient(u)
        abs_du = _abs_grad_kernel(du[0], du[1])
        aux_lt0, aux_gt0 = _fused_variance_kernel(
            image, c1, c0, lambda1, lambda2, abs_du
        )
        u[aux_lt0] = 1
        u[aux_gt0] = 0

        # Smoothing
        for _ in range(smoothing):
            u = _curvop(u, footprints, workspace)

        iter_callback(u)

    return u


@deprecate_kwarg({'iterations': 'num_iter'},
                 removed_version="23.02.00",
                 deprecated_version="22.02.00")
def morphological_geodesic_active_contour(gimage, num_iter,
                                          init_level_set='disk', smoothing=1,
                                          threshold='auto', balloon=0,
                                          iter_callback=lambda x: None):
    """Morphological Geodesic Active Contours (MorphGAC).

    Geodesic active contours implemented with morphological operators. It can
    be used to segment objects with visible but noisy, cluttered, broken
    borders.

    Parameters
    ----------
    gimage : (M, N) or (L, M, N) array
        Preprocessed image or volume to be segmented. This is very rarely the
        original image. Instead, this is usually a preprocessed version of the
        original image that enhances and highlights the borders (or other
        structures) of the object to segment.
        `morphological_geodesic_active_contour` will try to stop the contour
        evolution in areas where `gimage` is small. See
        `morphsnakes.inverse_gaussian_gradient` as an example function to
        perform this preprocessing. Note that the quality of
        `morphological_geodesic_active_contour` might greatly depend on this
        preprocessing.
    num_iter : uint
        Number of num_iter to run.
    init_level_set : str, (M, N) array, or (L, M, N) array
        Initial level set. If an array is given, it will be binarized and used
        as the initial level set. If a string is given, it defines the method
        to generate a reasonable initial level set with the shape of the
        `image`. Accepted values are 'checkerboard' and 'disk'. See the
        documentation of `checkerboard_level_set` and `disk_level_set`
        respectively for details about how these level sets are created.
    smoothing : uint, optional
        Number of times the smoothing operator is applied per iteration.
        Reasonable values are around 1-4. Larger values lead to smoother
        segmentations.
    threshold : float, optional
        Areas of the image with a value smaller than this threshold will be
        considered borders. The evolution of the contour will stop in this
        areas.
    balloon : float, optional
        Balloon force to guide the contour in non-informative areas of the
        image, i.e., areas where the gradient of the image is too small to push
        the contour towards a border. A negative value will shrink the contour,
        while a positive value will expand the contour in these areas. Setting
        this to zero will disable the balloon force.
    iter_callback : function, optional
        If given, this function is called once per iteration with the current
        level set as the only argument. This is useful for debugging or for
        plotting intermediate results during the evolution.

    Returns
    -------
    out : (M, N) or (L, M, N) array
        Final segmentation (i.e., the final level set)

    See Also
    --------
    inverse_gaussian_gradient, disk_level_set, checkerboard_level_set

    Notes
    -----
    This is a version of the Geodesic Active Contours (GAC) algorithm that uses
    morphological operators instead of solving partial differential equations
    (PDEs) for the evolution of the contour. The set of morphological operators
    used in this algorithm are proved to be infinitesimally equivalent to the
    GAC PDEs (see [1]_). However, morphological operators are do not suffer
    from the numerical stability issues typically found in PDEs (e.g., it is
    not necessary to find the right time step for the evolution), and are
    computationally faster.

    The algorithm and its theoretical derivation are described in [1]_.

    References
    ----------
    .. [1] A Morphological Approach to Curvature-based Evolution of Curves and
           Surfaces, Pablo Márquez-Neila, Luis Baumela, Luis Álvarez. In IEEE
           Transactions on Pattern Analysis and Machine Intelligence (PAMI),
           2014, :DOI:`10.1109/TPAMI.2013.106`
    """

    image = gimage
    init_level_set = _init_level_set(init_level_set, image.shape)

    _check_input(image, init_level_set)

    if threshold == 'auto':
        threshold = cp.percentile(image, 40)

    structure = cp.ones((3,) * len(image.shape), dtype=cp.int8)
    dimage = gradient(image)
    # threshold_mask = image > threshold
    if balloon != 0:
        threshold_mask_balloon = image > threshold / cp.abs(balloon)

    u = (init_level_set > 0).astype(cp.int8)

    if _misc.ndim(u) == 2:
        footprints = _get_P2()
    elif _misc.ndim(u) == 3:
        footprints = _get_P3()
    else:
        raise ValueError("u has an invalid number of dimensions "
                         "(should be 2 or 3)")
    workspace = cp.empty(((len(footprints),) + u.shape), dtype=u.dtype)

    iter_callback(u)

    for _ in range(num_iter):

        # Balloon
        if balloon > 0:
            aux = ndi.binary_dilation(u, structure)
        elif balloon < 0:
            aux = ndi.binary_erosion(u, structure)
        if balloon != 0:
            u[threshold_mask_balloon] = aux[threshold_mask_balloon]

        # Image attachment
        aux = cp.zeros_like(image)
        du = gradient(u)
        for el1, el2 in zip(dimage, du):
            aux += el1 * el2
        u[aux > 0] = 1
        u[aux < 0] = 0

        # Smoothing
        for _ in range(smoothing):
            u = _curvop(u, footprints, workspace)

        iter_callback(u)

    return u

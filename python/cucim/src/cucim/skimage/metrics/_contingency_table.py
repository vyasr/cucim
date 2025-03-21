import cupy as cp
import cupyx.scipy.sparse as sparse

__all__ = ['contingency_table']


def contingency_table(im_true, im_test, *, ignore_labels=None,
                      normalize=False):
    """
    Return the contingency table for all regions in matched segmentations.

    Parameters
    ----------
    im_true : ndarray of int
        Ground-truth label image, same shape as im_test.
    im_test : ndarray of int
        Test image.
    ignore_labels : sequence of int, optional
        Labels to ignore. Any part of the true image labeled with any of these
        values will not be counted in the score.
    normalize : bool
        Determines if the contingency table is normalized by pixel count.

    Returns
    -------
    cont : scipy.sparse.csr_matrix
        A contingency table. `cont[i, j]` will equal the number of voxels
        labeled `i` in `im_true` and `j` in `im_test`.
    """

    im_test_r = im_test.reshape(-1)
    im_true_r = im_true.reshape(-1)
    if ignore_labels is not None:
        ignore_labels = cp.asarray(ignore_labels)
        data = cp.isin(im_true_r, ignore_labels, invert=True).astype(float)
        if normalize:
            data /= cp.count_nonzero(data)
    else:
        if normalize:
            data = cp.full((im_test_r.size,), 1 / im_test_r.size, dtype=float)
        else:
            data = cp.ones((im_test_r.size,), dtype=float)
    cont = sparse.coo_matrix((data, (im_true_r, im_test_r))).tocsr()
    return cont

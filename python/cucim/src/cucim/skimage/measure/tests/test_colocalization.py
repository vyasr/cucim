import cupy as cp
import numpy as np
import pytest

from cucim.skimage.measure import (intersection_coeff, manders_coloc_coeff,
                                   manders_overlap_coeff, pearson_corr_coeff)


def test_invalid_input():
    # images are not same size
    img1 = cp.array([[i + j for j in range(4)] for i in range(4)])
    img2 = cp.ones((3, 5, 6))
    mask = cp.array([[i <= 1 for i in range(5)] for _ in range(5)])
    non_binary_mask = cp.array([[2 for __ in range(4)] for _ in range(4)])

    with pytest.raises(ValueError, match=". must have the same dimensions"):
        pearson_corr_coeff(img1, img1, mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        pearson_corr_coeff(img1, img2)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        pearson_corr_coeff(img1, img1, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        pearson_corr_coeff(img1, img1, non_binary_mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        manders_coloc_coeff(img1, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        manders_coloc_coeff(img1, non_binary_mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        manders_coloc_coeff(img1, img1 > 0, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        manders_coloc_coeff(img1, img1 > 0, non_binary_mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        manders_overlap_coeff(img1, img1, mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        manders_overlap_coeff(img1, img2)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        manders_overlap_coeff(img1, img1, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        manders_overlap_coeff(img1, img1, non_binary_mask)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        intersection_coeff(img1 > 2, img2 > 1, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        intersection_coeff(img1, img2)
    with pytest.raises(ValueError, match=". must have the same dimensions"):
        intersection_coeff(img1 > 2, img1 > 1, mask)
    with pytest.raises(ValueError, match=". array is not of dtype boolean"):
        intersection_coeff(img1 > 2, img1 > 1, non_binary_mask)


def test_pcc():
    # simple example
    img1 = cp.array([[i + j for j in range(4)] for i in range(4)])
    np.testing.assert_allclose(
        pearson_corr_coeff(img1, img1),
        (1.0, 0.0),
        rtol=1e-12,
    )

    img2 = cp.where(img1 <= 2, 0, img1)
    np.testing.assert_allclose(
        pearson_corr_coeff(img1, img2),
        (0.944911182523068, 3.5667540654536515e-08),
        rtol=1e-12,
    )

    # change background of roi and see if values are same
    roi = cp.where(img1 <= 2, 0, 1)
    np.testing.assert_allclose(
        pearson_corr_coeff(img1, img1, roi),
        pearson_corr_coeff(img1, img2, roi),
        rtol=1e-12,
    )


def test_mcc():
    img1 = cp.array([[j for j in range(4)] for i in range(4)])
    mask = cp.array([[i <= 1 for j in range(4)]for i in range(4)])
    assert manders_coloc_coeff(img1, mask) == 0.5

    # test negative values
    img_negativeint = cp.where(img1 == 1, -1, img1)
    img_negativefloat = img_negativeint / 2.0
    with pytest.raises(ValueError):
        manders_coloc_coeff(img_negativeint, mask)
    with pytest.raises(ValueError):
        manders_coloc_coeff(img_negativefloat, mask)


def test_moc():
    img1 = cp.ones((4, 4))
    img2 = 2 * cp.ones((4, 4))
    assert manders_overlap_coeff(img1, img2) == 1

    # test negative values
    img_negativeint = cp.where(img1 == 1, -1, img1)
    img_negativefloat = img_negativeint / 2.0
    with pytest.raises(ValueError):
        manders_overlap_coeff(img_negativeint, img2)
    with pytest.raises(ValueError):
        manders_overlap_coeff(img1, img_negativeint)
    with pytest.raises(ValueError):
        manders_overlap_coeff(img_negativefloat, img2)
    with pytest.raises(ValueError):
        manders_overlap_coeff(img1, img_negativefloat)
    with pytest.raises(ValueError):
        manders_overlap_coeff(img_negativefloat, img_negativefloat)


def test_intersection_coefficient():
    img1_mask = cp.array([[j <= 1 for j in range(4)] for i in range(4)])
    img2_mask = cp.array([[i <= 1 for j in range(4)] for i in range(4)])
    img3_mask = cp.array([[1 for j in range(4)] for i in range(4)])
    assert intersection_coeff(img1_mask, img2_mask) == 0.5
    assert intersection_coeff(img1_mask, img3_mask) == 1

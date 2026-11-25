import pytest
from moto import mock_aws
import boto3
from mintd._s3_listing_ops import list_product_objects

@pytest.fixture
def s3():
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        yield s3

def test_list_objects_non_recursive_returns_top_level_only(s3):
    s3.put_object(Bucket="test-bucket", Key="prefix/a.csv", Body="data")
    s3.put_object(Bucket="test-bucket", Key="prefix/sub/b.csv", Body="data")
    
    result = list_product_objects(
        bucket="test-bucket",
        prefix="prefix/",
        endpoint="https://s3.us-east-1.amazonaws.com",
        sub_path=None,
        recursive=False,
        include_versions=False,
        aws_profile_name=None,
        s3_client_factory=lambda cfg, prof: s3
    )
    
    keys = [o.key for o in result.objects]
    assert "a.csv" in keys
    assert "sub/" in keys
    assert "sub/b.csv" not in keys

def test_list_objects_recursive_includes_subdirs(s3):
    s3.put_object(Bucket="test-bucket", Key="prefix/a.csv", Body="data")
    s3.put_object(Bucket="test-bucket", Key="prefix/sub/b.csv", Body="data")
    
    result = list_product_objects(
        bucket="test-bucket",
        prefix="prefix/",
        endpoint="https://s3.us-east-1.amazonaws.com",
        sub_path=None,
        recursive=True,
        include_versions=False,
        aws_profile_name=None,
        s3_client_factory=lambda cfg, prof: s3
    )
    
    keys = [o.key for o in result.objects]
    assert "a.csv" in keys
    assert "sub/b.csv" in keys

def test_invalid_sub_path_rejected(s3):
    with pytest.raises(ValueError, match="invalid sub_path"):
        list_product_objects(
            bucket="test-bucket",
            prefix="prefix/",
            endpoint="https://s3.us-east-1.amazonaws.com",
            sub_path="..",
            recursive=True,
            include_versions=False,
            aws_profile_name=None,
            s3_client_factory=lambda cfg, prof: s3
        )


@pytest.mark.parametrize("sub_path", ["c:dir/", "q:notes.txt", "a..b", "a\x00b"],
                         ids=["drive", "drive-file", "dots-inside-a-name", "nul"])
def test_a_drive_dots_or_a_nul_is_no_sub_path(sub_path):
    """A first name Windows reads as a drive is outside, as for any recorded path;
    `..` is refused anywhere, even inside a name, as before; a NUL is refused.

    Mutations: drop the drive rule -> `c:dir/` passes, red; test `..` only as a
    segment -> `a..b` passes, red."""
    from mintd._s3_listing_ops import _normalise_sub_path

    with pytest.raises(ValueError, match="invalid sub_path"):
        _normalise_sub_path(sub_path)

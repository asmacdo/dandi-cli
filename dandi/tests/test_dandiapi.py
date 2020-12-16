import os.path
from ..dandiapi import DandiAPIClient


def test_upload(local_docker_compose, simple1_nwb, tmp_path):
    client = DandiAPIClient(
        api_url=local_docker_compose["instance"].api,
        token=local_docker_compose["django_api_key"],
    )
    with client.session():
        r = client.create_dandiset(name="Upload Test", metadata={})
        dandiset_id = r["identifier"]
        client.upload(dandiset_id, "draft", "testing/simple1.nwb", {}, simple1_nwb)
        asset, = client.get_dandiset_assets(dandiset_id, "draft")
        assert asset["path"] == "testing/simple1.nwb"
        client.download(dandiset_id, tmp_path)
        p, = tmp_path.glob("*/*")
        assert p == tmp_path / "testing" / "simple1.nwb"
        assert p.stat().st_size == os.path.getsize(simple1_nwb)

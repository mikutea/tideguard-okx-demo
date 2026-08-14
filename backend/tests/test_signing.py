from okx_demo_lab.okx_client import OkxClient


def test_signature_matches_known_vector() -> None:
    signature = OkxClient.sign(
        "22582BD0CFF14C41EDBF1AB98506286D",
        "2020-12-08T09:08:57.715Z",
        "GET",
        "/api/v5/account/balance?ccy=BTC",
    )
    # Independently cross-checked with Node's crypto.createHmac implementation.
    assert signature == "HiZhvSfMtWJA3uUIVXV3a/bSXNPCWvYFXoGCVS8V4zY="

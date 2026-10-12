import io

from PIL import Image

from linc_nerve.core.android import AndroidController
from linc_nerve.core.wda import WDASession


def test_get_scaled_image():
    # Create a large dummy image
    img = Image.new("RGB", (3000, 3000), color="red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    result = AndroidController._get_scaled_image(data)
    assert len(result) < len(data)
    assert isinstance(result, bytes)


def test_extract_session_id():
    session = WDASession("serial", 8100)
    assert session._extract_session_id({"sessionId": "123"}) == "123"
    assert session._extract_session_id({"value": {"sessionId": "456"}}) == "456"
    assert session._extract_session_id({"value": {"sessionId": ""}}) is None
    assert session._extract_session_id({"value": "not-a-dict"}) is None
    assert session._extract_session_id({"other": "key"}) is None

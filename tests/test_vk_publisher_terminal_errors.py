"""VK publisher terminal auth/capability classification tests."""

from unittest.mock import MagicMock, patch

from services.content_publishers.vk_publisher import VKPublisher


def _item(media_urls):
    item = MagicMock()
    item.id = 101
    item.body_text = "Synthetic post"
    item.get_hashtags.return_value = []
    item.get_media_urls.return_value = list(media_urls)
    return item


def _account(**credentials):
    account = MagicMock()
    account.account_id = "12345"
    account.get_credentials_dict.return_value = {
        "access_token": "group-token",
        "group_id": "12345",
        **credentials,
    }
    return account


def test_vk_publish_stops_after_first_terminal_photo_error():
    publisher = VKPublisher()
    publisher._upload_photo = MagicMock(return_value=(
        None,
        "Токен VK недействителен",
        "vk_auth_failed",
        True,
    ))

    result = publisher.publish(
        _item(["https://example.test/1.jpg", "https://example.test/2.jpg"]),
        _account(),
    )

    assert result.success is False
    assert result.error_code == "vk_user_token_required"
    assert result.terminal is True
    publisher._upload_photo.assert_called_once()


@patch(
    "services.content_publishers.vk_publisher._download_and_convert_to_jpeg",
    return_value=(b"jpeg", "photo.jpg"),
)
@patch("services.content_publishers.vk_publisher.requests.get")
def test_vk_upload_classifies_group_auth_error_27(_get, _download):
    _get.return_value.json.return_value = {
        "error": {
            "error_code": 27,
            "error_msg": "provider text is not a control contract",
        },
    }

    attachment, error, error_code, terminal = VKPublisher()._upload_photo(
        "group-token", "12345", "https://example.test/1.jpg", "5.199",
    )

    assert attachment is None
    assert "user_token" in error
    assert error_code == "vk_user_token_required"
    assert terminal is True


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_wall_post_classifies_invalid_token_as_terminal(post):
    post.return_value.json.return_value = {
        "error": {
            "error_code": 5,
            "error_msg": "invalid token provider detail",
        },
    }

    result = VKPublisher().publish(_item([]), _account())

    assert result.success is False
    assert result.error_code == "vk_auth_failed"
    assert result.terminal is True
    assert "provider detail" not in result.error

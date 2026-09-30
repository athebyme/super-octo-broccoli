"""VK publisher terminal auth/capability classification tests."""

from unittest.mock import MagicMock, patch

import requests

from services.content_publishers.vk_publisher import VKPublisher


def _item(media_urls):
    item = MagicMock()
    item.id = 101
    item.body_text = "Synthetic post"
    item.get_hashtags.return_value = []
    item.get_media_urls.return_value = list(media_urls)
    item.get_product_ids.return_value = []
    item.get_entity_refs.return_value = []
    item.get_platform_specific.return_value = {
        "product_url": "https://www.wildberries.ru/catalog/123/detail.aspx",
    }
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
        _account(user_token="legacy-user-token"),
    )

    assert result.success is False
    assert result.error_code == "vk_auth_failed"
    assert result.terminal is True
    publisher._upload_photo.assert_called_once()


@patch(
    "services.content_publishers.vk_publisher._download_and_convert_to_jpeg",
    return_value=(b"jpeg", "photo.jpg"),
)
@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_upload_classifies_group_auth_error_27(post, _download):
    post.return_value.json.return_value = {
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
def test_vk_community_key_publishes_editorial_link_without_photo_upload(post):
    post.return_value.json.return_value = {"response": {"post_id": 321}}
    publisher = VKPublisher()
    publisher._upload_photo = MagicMock()
    item = _item([])

    result = publisher.publish(item, _account())

    assert result.success is True
    assert result.external_post_url == "https://vk.com/wall-12345_321"
    assert result.error is None
    publisher._upload_photo.assert_not_called()
    assert post.call_args.args[0].endswith("/wall.post")
    payload = post.call_args.kwargs["data"]
    assert payload["message"].endswith(
        "https://www.wildberries.ru/catalog/123/detail.aspx"
    )
    assert "attachments" not in payload
    assert payload["owner_id"] == "-12345"
    assert len(payload["guid"]) == 32
    assert post.call_args.kwargs["allow_redirects"] is False


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_product_post_never_loses_its_photo_with_community_key(post):
    publisher = VKPublisher()
    publisher._upload_photo = MagicMock()
    item = _item(["https://example.test/1.jpg"])
    item.get_product_ids.return_value = [101]

    result = publisher.publish(item, _account())

    assert result.success is False
    assert result.error_code == "vk_user_token_required"
    assert result.terminal is True
    assert "без изображения не опубликован" in result.error
    publisher._upload_photo.assert_not_called()
    post.assert_not_called()


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_product_post_without_saved_media_also_requires_photo_access(post):
    item = _item([])
    item.get_product_ids.return_value = [101]

    result = VKPublisher().publish(item, _account())

    assert result.success is False
    assert result.error_code == "vk_user_token_required"
    post.assert_not_called()


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_does_not_attach_untrusted_product_link(post):
    post.return_value.json.return_value = {"response": {"post_id": 322}}
    item = _item([])
    item.get_platform_specific.return_value = {
        "product_url": "https://evil.test/catalog/123",
    }

    result = VKPublisher().publish(item, _account())

    assert result.success is True
    assert "attachments" not in post.call_args.kwargs["data"]
    assert "evil.test" not in post.call_args.kwargs["data"]["message"]


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_permission_check_requires_wall_and_sanitizes_errors(post):
    post.side_effect = [
        MagicMock(json=MagicMock(return_value={"response": {"groups": [{"id": 12345}]}})),
        MagicMock(json=MagicMock(return_value={
            "response": {"permissions": [{"name": "photos", "setting": 4}]},
        })),
    ]

    valid, error = VKPublisher().validate_account(_account())

    assert valid is False
    assert "стене" in error
    assert post.call_count == 2


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_permission_check_rejects_key_for_other_community(post):
    post.return_value.json.return_value = {
        "response": {"groups": [{"id": 98765}]},
    }

    valid, error = VKPublisher().validate_account(_account())

    assert valid is False
    assert "сообществу" in error
    assert post.call_count == 1


@patch("services.content_publishers.vk_publisher.requests.post")
def test_vk_wall_timeout_is_reported_as_unknown_outcome(post):
    post.side_effect = requests.exceptions.Timeout()

    result = VKPublisher().publish(_item([]), _account())

    assert result.success is False
    assert result.error_code == "vk_outcome_unknown"
    assert "Проверьте стену" in result.error


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

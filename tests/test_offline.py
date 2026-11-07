"""pytest 兼容入口：真正的用例在 pan115/selftest.py（随包分发）。

    pytest tests/            # 或
    python -m pan115 selftest
"""

import pan115.selftest as st


def test_pkce():
    st.test_pkce()


def test_token_bucket():
    st.test_token_bucket()


def test_auth_code_detection():
    st.test_auth_code_detection()


def test_fs_list_parsing():
    st.test_fs_list_parsing()


def test_folder_info_array_shape():
    st.test_folder_info_array_shape()


def test_rb_list_parsing():
    st.test_rb_list_parsing()


def test_normalize_callback():
    st.test_normalize_callback()


def test_oss_signature():
    st.test_oss_signature()


def test_upload_fast_pass():
    st.test_upload_fast_pass()


def test_upload_sign_check():
    st.test_upload_sign_check()


def test_sha1_range():
    st.test_sha1_range()


def test_config_roundtrip():
    st.test_config_roundtrip()


def test_cli_parser():
    st.test_cli_parser()


def test_client_success_and_error():
    st.test_client_success_and_error()


def test_client_auto_refresh():
    st.test_client_auto_refresh()


def test_client_retry_on_5xx():
    st.test_client_retry_on_5xx()


def test_client_auth_refresh_failure():
    st.test_client_auth_refresh_failure()

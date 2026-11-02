"""Tencent's official SDK adapter. No implicit calls, fallback or paid retries."""
import base64
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import stat

from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.retry import NoopRetryer
from tencentcloud.ocr.v20181119.models import GeneralAccurateOCRRequest
from tencentcloud.ocr.v20181119.ocr_client import OcrClient

ERRORS = {
    'NotConfigured': '尚未启用 OCR 或配置可用凭证，请先完成服务器配置。',
    'AuthFailure': 'OCR 身份或签名校验失败，请检查服务端凭证。',
    'UnauthorizedOperation': '当前凭证没有所需 OCR 权限，请检查 CAM 授权。',
    'LimitExceeded': 'OCR 额度或账户限制，请检查腾讯云控制台。',
    'ResourceUnavailable': 'OCR 服务暂不可用，请稍后手动重试。',
    'RequestLimitExceeded': 'OCR 请求过于频繁，请稍后手动重试。',
    'InvalidParameter': 'OCR 不接受这张图片或当前参数，请检查图片范围和尺寸。',
    'NoText': '没有识别出文字。请检查选区是否清晰，或直接手动填写原文。',
    'InvalidResponse': 'OCR 返回的文字无效或超过 50000 字符，请缩小识别范围。',
    'NetworkError': 'OCR 网络请求失败或超时，结果可能不确定；重试可能再次计费，请确认后手动操作。',
    'Interrupted': '上次识别未能确认完成，系统没有自动重试；再次识别可能再次计费。',
    'InternalError': '识别未能完成，请检查服务状态后手动重试。',
    'ImageUnavailable': '来源图片缺失或校验失败，未提交 OCR，请重新上传。',
}


class OCRFailure(Exception):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'InternalError'
        super().__init__(ERRORS[self.code])


@dataclass(frozen=True)
class OCRResult:
    text: str
    response_json: str
    request_id: str


def normalize_response(response):
    """Preserve provider text/line order; no guessing, rewriting or Markdown parsing."""
    if not isinstance(response, dict) or not isinstance(response.get('TextDetections'), list):
        raise OCRFailure('InvalidResponse')
    lines = response['TextDetections']
    if any(not isinstance(line, dict) or not isinstance(line.get('DetectedText'), str) for line in lines):
        raise OCRFailure('InvalidResponse')
    text = '\n'.join(line['DetectedText'] for line in lines)
    if not text.strip():
        raise OCRFailure('NoText')
    if len(text) > 50000 or '\x00' in text:
        raise OCRFailure('InvalidResponse')
    # Store only OCR result fields, never requests, headers or credentials.
    safe = {'TextDetections': [{key: line[key] for key in ('DetectedText', 'Confidence', 'Polygon') if key in line} for line in lines]}
    request_id = response.get('RequestId', '')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{0,128}', request_id):
        request_id = ''
    safe['RequestId'] = request_id
    encoded = json.dumps(safe, ensure_ascii=False)
    if len(encoded.encode('utf-8')) > 1024 * 1024:
        raise OCRFailure('InvalidResponse')
    return OCRResult(text, encoded, request_id)


class TencentOCR:
    def configured(self):
        return os.environ.get('NOTESHUB_OCR_ENABLED', '').lower() == 'true' and bool(
            os.environ.get('NOTESHUB_TENCENT_CREDENTIALS_FILE') or (
                os.environ.get('NOTESHUB_TENCENT_SECRET_ID') and os.environ.get('NOTESHUB_TENCENT_SECRET_KEY')))

    def _credential(self):
        if not self.configured():
            raise OCRFailure('NotConfigured')
        try:
            path = os.environ.get('NOTESHUB_TENCENT_CREDENTIALS_FILE')
            if path:
                file = Path(path)
                metadata = file.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16384 or metadata.st_mode & 0o077:
                    raise OCRFailure('NotConfigured')
                data = json.loads(file.read_text(encoding='utf-8'))
                secret_id, secret_key, token = data.get('SecretId'), data.get('SecretKey'), data.get('Token')
            else:
                secret_id = os.environ.get('NOTESHUB_TENCENT_SECRET_ID')
                secret_key = os.environ.get('NOTESHUB_TENCENT_SECRET_KEY')
                token = os.environ.get('NOTESHUB_TENCENT_TOKEN')
            if not all(isinstance(value, str) and value.strip() for value in (secret_id, secret_key)):
                raise OCRFailure('NotConfigured')
            if token is not None and not isinstance(token, str):
                raise OCRFailure('NotConfigured')
            return Credential(secret_id, secret_key, token)
        except (OSError, ValueError, AttributeError):
            raise OCRFailure('NotConfigured') from None

    def prepare(self):
        # Validate local configuration before creating a durable billable job.
        self._credential()

    def recognize(self, raw):
        credential = self._credential()
        http = HttpProfile(endpoint='ocr.tencentcloudapi.com', reqTimeout=30, protocol='https', reqMethod='POST')
        # Respect inherited CA configuration; never disable TLS verification.
        http.certification = os.environ.get('REQUESTS_CA_BUNDLE') or os.environ.get('SSL_CERT_FILE') or None
        profile = ClientProfile(httpProfile=http, retryer=NoopRetryer(), disable_region_breaker=True)
        # The SDK's debug formatter includes request bodies/auth headers.
        logging.getLogger('tencentcloud_sdk_common').setLevel(logging.WARNING)
        client = OcrClient(credential, os.environ.get('NOTESHUB_TENCENT_REGION', 'ap-guangzhou'), profile)
        request = GeneralAccurateOCRRequest()
        request.ImageBase64 = base64.b64encode(raw).decode('ascii')
        try:
            response = client.GeneralAccurateOCR(request)
            return normalize_response(json.loads(response.to_json_string()))
        except TencentCloudSDKException as exc:
            code = (exc.get_code() or '').split('.')[0]
            if code in ('ClientNetworkError', 'ServerNetworkError', 'ClientError'):
                code = 'NetworkError'
            raise OCRFailure(code) from None
        except OCRFailure:
            raise
        except Exception:
            # Exception text may contain signed headers, secrets or image data.
            raise OCRFailure('InternalError') from None

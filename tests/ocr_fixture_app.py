"""Local browser-test factory only; excluded from deployment archives."""
import time
from fastapi.responses import JSONResponse
from noteshub.ocr import OCRFailure, normalize_response
from noteshub.web import create_app as real_app


class BrowserOCR:
    def __init__(self):
        self.calls = 0

    def configured(self):
        return True

    def prepare(self):
        pass

    def recognize(self, raw):
        self.calls += 1
        number = self.calls
        time.sleep(1.5)
        if number == 1:
            raise OCRFailure('NetworkError')
        text = '模拟识别的中文原文' if number == 2 else '模拟重新识别的候选原文'
        return normalize_response({'TextDetections':[{'DetectedText':text}, {'DetectedText':'第二行 <script>window.ocrInjected=true</script>'}], 'RequestId':f'mock-{number}'})


def application():
    provider = BrowserOCR()
    app = real_app(ocr_client=provider)

    @app.get('/__test__/ocr-count')
    def count():
        return JSONResponse({'mock_ocr_calls':provider.calls})

    return app

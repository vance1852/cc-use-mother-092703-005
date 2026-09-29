"""再制造服务向 API 和 CLI 暴露的稳定错误。"""


class RemanufactureError(RuntimeError):
    code = "remanufacture_error"
    status = 400


class NotFound(RemanufactureError):
    code = "not_found"
    status = 404


class Conflict(RemanufactureError):
    code = "conflict"
    status = 409


class Forbidden(RemanufactureError):
    code = "forbidden"
    status = 403


class InvalidState(RemanufactureError):
    code = "invalid_state"
    status = 409


class ValidationFailed(RemanufactureError):
    code = "validation_failed"
    status = 422

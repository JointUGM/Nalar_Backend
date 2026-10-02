from collections.abc import Mapping


class AppError(Exception):
    code = "INTERNAL"
    message = "Terjadi kesalahan pada server."

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code or type(self).code
        self.message = message or type(self).message
        self.details: dict[str, object] = dict(details or {})
        super().__init__(self.code)


class InvalidInput(AppError):
    code = "INVALID_INPUT"
    message = "Data yang dikirim tidak valid."


class InvalidActivation(InvalidInput):
    code = "INVALID_ACTIVATION"
    message = "Tautan aktivasi tidak valid atau sudah kedaluwarsa."


class Unauthenticated(AppError):
    code = "UNAUTHENTICATED"
    message = "Silakan masuk terlebih dahulu."


class InvalidCredentials(Unauthenticated):
    code = "INVALID_CREDENTIALS"
    message = "Email atau kata sandi salah."


class Forbidden(AppError):
    code = "FORBIDDEN"
    message = "Kamu tidak punya akses untuk tindakan ini."


class NotFound(AppError):
    code = "NOT_FOUND"
    message = "Data tidak ditemukan."


class Conflict(AppError):
    code = "CONFLICT"
    message = "Permintaan bertentangan dengan keadaan saat ini."


class Unprocessable(AppError):
    code = "UNPROCESSABLE"
    message = "Data tidak dapat diproses."


class TooManyRequests(AppError):
    code = "RATE_LIMITED"
    message = "Terlalu banyak percobaan. Tunggu sebentar lalu coba lagi."


class DependencyUnavailable(AppError):
    code = "DEPENDENCY_UNAVAILABLE"
    message = "Layanan sedang tidak tersedia. Coba lagi sebentar."

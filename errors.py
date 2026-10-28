"""Safe user-facing failures; exception details must not expose document text."""


class TranslationError(Exception):
    pass

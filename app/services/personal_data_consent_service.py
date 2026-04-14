import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.personal_data_consent import (
    get_personal_data_consent,
    set_personal_data_consent_enabled,
    upsert_personal_data_consent,
)
from app.database.models import PersonalDataConsent


logger = structlog.get_logger(__name__)


class PersonalDataConsentService:
    """Helpers for managing the personal data consent text and visibility."""

    MAX_PAGE_LENGTH = 3500

    @staticmethod
    def _normalize_language(language: str) -> str:
        base_language = language or settings.DEFAULT_LANGUAGE or 'ru'
        return base_language.split('-')[0].lower()

    @staticmethod
    def normalize_language(language: str) -> str:
        return PersonalDataConsentService._normalize_language(language)

    @classmethod
    async def get_consent(
        cls,
        db: AsyncSession,
        language: str,
        *,
        fallback: bool = False,
    ) -> PersonalDataConsent | None:
        lang = cls._normalize_language(language)
        consent = await get_personal_data_consent(db, lang)

        if consent or not fallback:
            return consent

        default_lang = cls._normalize_language(settings.DEFAULT_LANGUAGE)
        if lang != default_lang:
            return await get_personal_data_consent(db, default_lang)

        return consent

    @classmethod
    async def save_consent(
        cls,
        db: AsyncSession,
        language: str,
        content: str,
    ) -> PersonalDataConsent:
        lang = cls._normalize_language(language)
        consent = await upsert_personal_data_consent(db, lang, content, enable_if_new=True)
        logger.info('✅ Согласие на обработку ПД обновлено для языка', lang=lang)
        return consent

    @classmethod
    async def set_enabled(
        cls,
        db: AsyncSession,
        language: str,
        enabled: bool,
    ) -> PersonalDataConsent:
        lang = cls._normalize_language(language)
        return await set_personal_data_consent_enabled(db, lang, enabled)

    @staticmethod
    def split_content_into_pages(
        content: str,
        *,
        max_length: int = None,
    ) -> list[str]:
        if not content:
            return []

        normalized = content.replace('\r\n', '\n').strip()
        if not normalized:
            return []

        max_len = max_length or PersonalDataConsentService.MAX_PAGE_LENGTH

        if len(normalized) <= max_len:
            return [normalized]

        paragraphs = [paragraph.strip() for paragraph in normalized.split('\n\n') if paragraph.strip()]

        pages: list[str] = []
        current = ''

        def flush_current() -> None:
            nonlocal current
            if current:
                pages.append(current.strip())
                current = ''

        for paragraph in paragraphs:
            candidate = f'{current}\n\n{paragraph}'.strip() if current else paragraph
            if len(candidate) <= max_len:
                current = candidate
                continue

            flush_current()

            if len(paragraph) <= max_len:
                current = paragraph
                continue

            start_index = 0
            while start_index < len(paragraph):
                chunk = paragraph[start_index : start_index + max_len]
                pages.append(chunk.strip())
                start_index += max_len

            current = ''

        flush_current()

        if not pages:
            return [normalized[:max_len]]

        return pages

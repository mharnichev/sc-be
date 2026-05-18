from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.google_business_review_cache import GoogleBusinessReviewCache
from app.schemas.review import GoogleBusinessReviewsResponse


class GoogleBusinessReviewsError(RuntimeError):
    pass


class GoogleBusinessReviewsService:
    cache_source = "google_business_profile"
    token_url = "https://oauth2.googleapis.com/token"
    reviews_base_url = "https://mybusiness.googleapis.com/v4"
    fallback_payload: dict[str, Any] = {
        "average_rating": 5.0,
        "total_review_count": 15,
        "items": [
            {
                "review_id": "soul-cuts-fallback-1",
                "name": "fallback/soul-cuts-serafim-brodskyi",
                "reviewer": {"display_name": "Серафім Бродський", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Стрижка голови, бороди та вусів у виконанні Гліба створила порядок із хаосу. "
                    "Якість на найвищому рівні. Інтер'єр, музика, напої - все відповідно. "
                    "Комфортна зона очікування. Єдиний мінус - не знав про це місце раніше, тепер тільки сюди."
                ),
                "create_time": "2025-10-18T00:00:00+00:00",
                "update_time": "2025-10-18T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Стрижка голови, бороди та вусів у виконанні Гліба створила порядок із хаосу. "
                        "Якість на найвищому рівні. Інтер'єр, музика, напої - все відповідно. "
                        "Комфортна зона очікування. Єдиний мінус - не знав про це місце раніше, тепер тільки сюди."
                    ),
                    "en": (
                        "A haircut, beard trim, and moustache trim by Gleb brought order out of chaos. "
                        "The quality is top level. Interior, music, and drinks all match the vibe. "
                        "Comfortable waiting area. The only downside is that I did not know about this place earlier. "
                        "Now this is the only place for me."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-2",
                "name": "fallback/soul-cuts-nikto-nikak",
                "reviewer": {"display_name": "Nikto Nikak", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Кайф, у холодильнику є газовані напої, музика приємна, а Гліб творить магію своїми руками.",
                "create_time": None,
                "update_time": None,
                "review_reply": None,
                "translations": {
                    "uk": "Кайф, у холодильнику є газовані напої, музика приємна, а Гліб творить магію своїми руками.",
                    "en": "Great vibe, cold sodas in the fridge, pleasant music, and Gleb works real magic with his hands.",
                },
            },
            {
                "review_id": "soul-cuts-fallback-3",
                "name": "fallback/soul-cuts-roman",
                "reviewer": {"display_name": "Роман", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Майстер Гліб просто молодець. Врахував усі побажання. Стрижка супер, саме як я хотів.",
                "create_time": "2026-01-23T00:00:00+00:00",
                "update_time": "2026-01-23T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": "Майстер Гліб просто молодець. Врахував усі побажання. Стрижка супер, саме як я хотів.",
                    "en": "Gleb is a great barber. He took all my wishes into account. The haircut is excellent, exactly what I wanted.",
                },
            },
            {
                "review_id": "soul-cuts-fallback-4",
                "name": "fallback/soul-cuts-michael-lezvie",
                "reviewer": {"display_name": "Michael Lezvie", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Радий, що знайшов справді крутого барбера, майстра своєї справи, який підлаштовується під "
                    "побажання клієнта і з яким можна порадитися. Результат на найвищому рівні, особливо задоволений "
                    "подовженою стрижкою. Далеко не всюди її можуть зробити нормально, а тут просто пушка."
                ),
                "create_time": "2025-08-27T00:00:00+00:00",
                "update_time": "2025-08-27T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Радий, що знайшов справді крутого барбера, майстра своєї справи, який підлаштовується під "
                        "побажання клієнта і з яким можна порадитися. Результат на найвищому рівні, особливо задоволений "
                        "подовженою стрижкою. Далеко не всюди її можуть зробити нормально, а тут просто пушка."
                    ),
                    "en": (
                        "I am glad I found a genuinely great barber who knows his craft, adapts to the client's wishes, "
                        "and gives helpful advice. The result is top level. I am especially happy with the longer haircut, "
                        "which not every place can do well, but here it turned out perfect."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-5",
                "name": "fallback/soul-cuts-alexey-grishin",
                "reviewer": {"display_name": "Алексей Гришин", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Гліб, мабуть, найкращий майстер в Одесі. Я пробував багатьох майстрів, але Гліб робить те, "
                    "що іншим навіть близько не під силу. Однозначно лайк!"
                ),
                "create_time": "2025-09-14T00:00:00+00:00",
                "update_time": "2025-09-14T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Гліб, мабуть, найкращий майстер в Одесі. Я пробував багатьох майстрів, але Гліб робить те, "
                        "що іншим навіть близько не під силу. Однозначно лайк!"
                    ),
                    "en": (
                        "Gleb is probably the best barber in Odesa. I have tried many barbers, but Gleb does things "
                        "that others cannot even come close to. Definitely a like!"
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-6",
                "name": "fallback/soul-cuts-dany-travel",
                "reviewer": {"display_name": "Dany Travel", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Найкраща стрижка за останній час! Без бороди, але з характером - майстер ідеально вловив мій стиль. "
                    "Чиста робота, крута атмосфера. Якщо шукаєте нормального барбера - однозначно сюди. 10/10"
                ),
                "create_time": "2026-02-03T00:00:00+00:00",
                "update_time": "2026-02-03T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Найкраща стрижка за останній час! Без бороди, але з характером - майстер ідеально вловив мій стиль. "
                        "Чиста робота, крута атмосфера. Якщо шукаєте нормального барбера - однозначно сюди. 10/10"
                    ),
                    "en": (
                        "The best haircut I have had lately. No beard, but plenty of character. The barber captured my style perfectly. "
                        "Clean work and a great atmosphere. If you are looking for a proper barber, this is definitely the place. 10/10"
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-7",
                "name": "fallback/soul-cuts-viktor-romanov",
                "reviewer": {"display_name": "Viktor Romanov", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "05.04.2026 вперше потрапив на стрижку до Гліба. Був приємно здивований, що людина, вперше бачачи "
                    "мене, з першого разу зробила красу. Однозначно рекомендую як майстра, і як людина він дуже приємний."
                ),
                "create_time": "2026-02-19T00:00:00+00:00",
                "update_time": "2026-02-19T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "05.04.2026 вперше потрапив на стрижку до Гліба. Був приємно здивований, що людина, вперше бачачи "
                        "мене, з першого разу зробила красу. Однозначно рекомендую як майстра, і як людина він дуже приємний."
                    ),
                    "en": (
                        "On 05.04.2026 I had my first haircut with Gleb. I was pleasantly surprised that, seeing me for the first time, "
                        "he made it look great on the first try. I definitely recommend him as a barber, and he is also a very pleasant person."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-8",
                "name": "fallback/soul-cuts-vovka-galamai",
                "reviewer": {"display_name": "Vovka Galamai", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Мені сподобався цей салон. Дуже приємний інтер'єр. Індивідуальний підхід до клієнта і бажання "
                    "повернутися наступного разу. Дякую."
                ),
                "create_time": "2026-03-29T00:00:00+00:00",
                "update_time": "2026-03-29T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Мені сподобався цей салон. Дуже приємний інтер'єр. Індивідуальний підхід до клієнта і бажання "
                        "повернутися наступного разу. Дякую."
                    ),
                    "en": (
                        "I liked this salon. Very pleasant interior, an individual approach to the client, and a real desire "
                        "to come back next time. Thank you."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-9",
                "name": "fallback/soul-cuts-vlad-belobrov",
                "reviewer": {"display_name": "Vlad Belobrov", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Надзвичайно задоволений стрижкою. Для мене було несподівано приємно, що майстер не лише дослухався "
                    "до моїх побажань, а й додав індивідуальний підхід. Дуже рекомендую цей заклад і, коли буду наступного "
                    "разу в Одесі, обов'язково приїду ще."
                ),
                "create_time": "2026-02-26T00:00:00+00:00",
                "update_time": "2026-02-26T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Надзвичайно задоволений стрижкою. Для мене було несподівано приємно, що майстер не лише дослухався "
                        "до моїх побажань, а й додав індивідуальний підхід. Дуже рекомендую цей заклад і, коли буду наступного "
                        "разу в Одесі, обов'язково приїду ще."
                    ),
                    "en": (
                        "I am extremely happy with the haircut. It was unexpectedly pleasant that the barber not only listened "
                        "to my wishes, but also added an individual touch. I highly recommend this place, and the next time I am "
                        "in Odesa, I will definitely come again."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-10",
                "name": "fallback/soul-cuts-pavel-grodzitskiy",
                "reviewer": {"display_name": "Павел Гродзицкий", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Побачив на мапах цей барбершоп біля дому, сходив на гоління і стрижку - все чітко!",
                "create_time": "2025-11-06T00:00:00+00:00",
                "update_time": "2025-11-06T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": "Побачив на мапах цей барбершоп біля дому, сходив на гоління і стрижку - все чітко!",
                    "en": "I saw this barbershop on the map near my home, went in for a shave and haircut, and everything was spot on.",
                },
            },
            {
                "review_id": "soul-cuts-fallback-11",
                "name": "fallback/soul-cuts-artem-atamaniuk",
                "reviewer": {"display_name": "Artem Atamaniuk", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Відгук - лише мої враження. Майстер Gleb запропонував щось написати, а я не проти поділитися. "
                    "Місце без пафосу, але приємне і стильне, як на мене. Знайшов випадково, без рекомендацій, через "
                    "Google Maps, бо було поруч. Сподобалася назва Soul Cuts - якось душевно. Потрапив випадково до "
                    "Gleb, який зрозумів мої побажання щодо зачіски без зайвих пояснень і візуалізацій. Звісно раджу, "
                    "це суб'єктивно, але варто відвідати цих хлопців заради їхнього стилю."
                ),
                "create_time": "2025-11-21T00:00:00+00:00",
                "update_time": "2025-11-21T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Відгук - лише мої враження. Майстер Gleb запропонував щось написати, а я не проти поділитися. "
                        "Місце без пафосу, але приємне і стильне, як на мене. Знайшов випадково, без рекомендацій, через "
                        "Google Maps, бо було поруч. Сподобалася назва Soul Cuts - якось душевно. Потрапив випадково до "
                        "Gleb, який зрозумів мої побажання щодо зачіски без зайвих пояснень і візуалізацій. Звісно раджу, "
                        "це суб'єктивно, але варто відвідати цих хлопців заради їхнього стилю."
                    ),
                    "en": (
                        "This review is just my personal impression. Gleb suggested writing something, and I do not mind sharing. "
                        "The place is not pretentious, but it is pleasant and stylish in my view. I found it by chance, without "
                        "recommendations, through Google Maps because it was nearby. I liked the name Soul Cuts; it feels soulful. "
                        "I ended up with Gleb by chance, and he understood my haircut wishes without extra explanations or visual examples. "
                        "Of course I recommend it. It is subjective, but these guys are worth visiting for their style."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-12",
                "name": "fallback/soul-cuts-vladyslav-ukraintsev",
                "reviewer": {"display_name": "Владислав Украинцев", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Дуже професійні майстри, крута атмосфера і чудовий підхід до клієнтів. Рекомендую.",
                "create_time": "2025-10-04T00:00:00+00:00",
                "update_time": "2025-10-04T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": "Дуже професійні майстри, крута атмосфера і чудовий підхід до клієнтів. Рекомендую.",
                    "en": "Very professional barbers, a great atmosphere, and an excellent approach to clients. I recommend it.",
                },
            },
            {
                "review_id": "soul-cuts-fallback-13",
                "name": "fallback/soul-cuts-vladyslav-studenets",
                "reviewer": {"display_name": "Владислав Студенец", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Усе сподобалося! Буду ходити постійно, все на найвищому рівні! Дуже комфортно!",
                "create_time": "2025-10-24T00:00:00+00:00",
                "update_time": "2025-10-24T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": "Усе сподобалося! Буду ходити постійно, все на найвищому рівні! Дуже комфортно!",
                    "en": "I liked everything. I will keep coming regularly. Everything is top level and very comfortable.",
                },
            },
            {
                "review_id": "soul-cuts-fallback-14",
                "name": "fallback/soul-cuts-kostya-kostya",
                "reviewer": {"display_name": "Kostya Kostya", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": (
                    "Стрижуся в Soul Cuts уже довгий час, завжди все на висоті: топові стрижки, топовий настрій, "
                    "топова косметика, бомбезна музика - усе, що потрібно для чудового барбершопу."
                ),
                "create_time": "2025-10-30T00:00:00+00:00",
                "update_time": "2025-10-30T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": (
                        "Стрижуся в Soul Cuts уже довгий час, завжди все на висоті: топові стрижки, топовий настрій, "
                        "топова косметика, бомбезна музика - усе, що потрібно для чудового барбершопу."
                    ),
                    "en": (
                        "I have been getting my hair cut at Soul Cuts for a long time, and everything is always excellent: "
                        "great haircuts, great mood, great cosmetics, amazing music - everything a great barbershop needs."
                    ),
                },
            },
            {
                "review_id": "soul-cuts-fallback-15",
                "name": "fallback/soul-cuts-alexander-balev",
                "reviewer": {"display_name": "Александр Балев", "profile_photo_url": None, "is_anonymous": False},
                "star_rating": 5,
                "comment": "Рекомендую, сервіс і надання послуг на дуже високому рівні.",
                "create_time": "2025-08-09T00:00:00+00:00",
                "update_time": "2025-08-09T00:00:00+00:00",
                "review_reply": None,
                "translations": {
                    "uk": "Рекомендую, сервіс і надання послуг на дуже високому рівні.",
                    "en": "I recommend it. The service and quality of work are at a very high level.",
                },
            },
        ],
    }

    async def get_reviews(
        self,
        session: AsyncSession,
        *,
        force_refresh: bool = False,
    ) -> GoogleBusinessReviewsResponse:
        cache = await self._get_cache(session)
        now = datetime.now(timezone.utc)

        if cache and not force_refresh and self._is_fresh(cache, now):
            return self._response_from_cache(cache, stale=False)

        try:
            payload = await self._fetch_reviews()
        except GoogleBusinessReviewsError:
            if cache:
                return self._response_from_cache(cache, stale=True)
            return self._fallback_response()

        cache = await self._save_cache(session, cache, payload, now)
        return self._response_from_cache(cache, stale=False)

    async def _get_cache(self, session: AsyncSession) -> GoogleBusinessReviewCache | None:
        result = await session.execute(
            select(GoogleBusinessReviewCache).where(GoogleBusinessReviewCache.source == self.cache_source)
        )
        return result.scalar_one_or_none()

    def _is_fresh(self, cache: GoogleBusinessReviewCache, now: datetime) -> bool:
        fetched_at = self._ensure_timezone(cache.fetched_at)
        return fetched_at >= now - timedelta(days=settings.google_business_reviews_cache_ttl_days)

    async def _save_cache(
        self,
        session: AsyncSession,
        cache: GoogleBusinessReviewCache | None,
        payload: dict[str, Any],
        fetched_at: datetime,
    ) -> GoogleBusinessReviewCache:
        if cache is None:
            cache = GoogleBusinessReviewCache(source=self.cache_source, payload=payload, fetched_at=fetched_at)
            session.add(cache)
        else:
            cache.payload = payload
            cache.fetched_at = fetched_at

        await session.commit()
        await session.refresh(cache)
        return cache

    def _response_from_cache(self, cache: GoogleBusinessReviewCache, *, stale: bool) -> GoogleBusinessReviewsResponse:
        fetched_at = self._ensure_timezone(cache.fetched_at)
        expires_at = fetched_at + timedelta(days=settings.google_business_reviews_cache_ttl_days)
        payload = dict(cache.payload)
        payload["fetched_at"] = fetched_at
        payload["cache_expires_at"] = expires_at
        payload["stale"] = stale
        return GoogleBusinessReviewsResponse.model_validate(payload)

    def _fallback_response(self) -> GoogleBusinessReviewsResponse:
        payload = dict(self.fallback_payload)
        payload["fetched_at"] = None
        payload["cache_expires_at"] = None
        payload["stale"] = True
        return GoogleBusinessReviewsResponse.model_validate(payload)

    def _ensure_timezone(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    async def _fetch_reviews(self) -> dict[str, Any]:
        self._validate_settings()
        access_token = await asyncio.to_thread(self._request_access_token)
        return await asyncio.to_thread(self._request_reviews, access_token)

    def _validate_settings(self) -> None:
        missing = [
            name
            for name, value in {
                "GOOGLE_BUSINESS_CLIENT_ID": settings.google_business_client_id,
                "GOOGLE_BUSINESS_CLIENT_SECRET": settings.google_business_client_secret,
                "GOOGLE_BUSINESS_REFRESH_TOKEN": settings.google_business_refresh_token,
                "GOOGLE_BUSINESS_ACCOUNT_ID": settings.google_business_account_id,
                "GOOGLE_BUSINESS_LOCATION_ID": settings.google_business_location_id,
            }.items()
            if not value
        ]
        if missing:
            raise GoogleBusinessReviewsError(f"Missing Google Business settings: {', '.join(missing)}")

    def _request_access_token(self) -> str:
        body = urlencode(
            {
                "client_id": settings.google_business_client_id,
                "client_secret": settings.google_business_client_secret,
                "refresh_token": settings.google_business_refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = Request(
            self.token_url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        data = self._send_json_request(request)
        access_token = data.get("access_token")
        if not access_token:
            raise GoogleBusinessReviewsError("Google OAuth response did not include access_token")
        return str(access_token)

    def _request_reviews(self, access_token: str) -> dict[str, Any]:
        page_size = max(1, min(settings.google_business_reviews_page_size, 50))
        max_pages = max(1, settings.google_business_reviews_max_pages)
        reviews: list[dict[str, Any]] = []
        next_page_token: str | None = None
        average_rating: float | None = None
        total_review_count = 0

        for _ in range(max_pages):
            query = {
                "pageSize": str(page_size),
                "orderBy": settings.google_business_reviews_order_by,
            }
            if next_page_token:
                query["pageToken"] = next_page_token

            url = f"{self.reviews_base_url}/{self._reviews_parent()}/reviews?{urlencode(query)}"
            request = Request(url, headers={"Authorization": f"Bearer {access_token}"}, method="GET")
            data = self._send_json_request(request)

            reviews.extend(self._normalize_review(review) for review in data.get("reviews", []))
            average_rating = data.get("averageRating", average_rating)
            total_review_count = data.get("totalReviewCount", total_review_count)
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break

        return {
            "average_rating": average_rating,
            "total_review_count": total_review_count,
            "items": reviews,
        }

    def _reviews_parent(self) -> str:
        account_id = str(settings.google_business_account_id).removeprefix("accounts/")
        location_id = str(settings.google_business_location_id).removeprefix("locations/")
        return f"accounts/{account_id}/locations/{location_id}"

    def _send_json_request(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GoogleBusinessReviewsError(f"Google API request failed with {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GoogleBusinessReviewsError(f"Google API request failed: {exc}") from exc

    def _normalize_review(self, review: dict[str, Any]) -> dict[str, Any]:
        reviewer = review.get("reviewer") or {}
        reply = review.get("reviewReply")
        return {
            "review_id": review.get("reviewId") or self._review_id_from_name(review.get("name")),
            "name": review.get("name"),
            "reviewer": {
                "display_name": reviewer.get("displayName"),
                "profile_photo_url": reviewer.get("profilePhotoUrl"),
                "is_anonymous": bool(reviewer.get("isAnonymous", False)),
            }
            if reviewer
            else None,
            "star_rating": self._star_rating_to_int(review.get("starRating")),
            "comment": review.get("comment"),
            "create_time": review.get("createTime"),
            "update_time": review.get("updateTime"),
            "review_reply": {
                "comment": reply.get("comment"),
                "update_time": reply.get("updateTime"),
            }
            if reply
            else None,
        }

    def _review_id_from_name(self, name: str | None) -> str:
        if not name:
            return ""
        return name.rsplit("/", 1)[-1]

    def _star_rating_to_int(self, value: str | int | None) -> int | None:
        if isinstance(value, int):
            return value
        return {
            "ONE": 1,
            "TWO": 2,
            "THREE": 3,
            "FOUR": 4,
            "FIVE": 5,
        }.get(str(value or "").upper())

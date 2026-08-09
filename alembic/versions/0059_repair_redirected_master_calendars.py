"""repair legacy rows stored on redirect source calendars

Revision ID: 0059_redirect_calendar_repair
Revises: 0058_funnel_no_slot_duration
Create Date: 2026-08-08 00:00:00.000000

This is an intentionally irreversible ownership repair.  A downgrade keeps the
repaired target-calendar ownership because guessing the former source would be
unsafe after redirect chains or schedules have changed.
"""

from __future__ import annotations

from alembic import op


revision = "0059_redirect_calendar_repair"
down_revision = "0058_funnel_no_slot_duration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This repair derives and then rewrites calendar ownership in several
    # statements.  Keep the source snapshot stable until the transaction
    # commits so normal booking/offer/calendar writes cannot race the checks.
    op.execute(
        """
        LOCK TABLE
            masters,
            barber_services,
            bookings,
            booking_service_items,
            waitlist_offers,
            master_time_blocks,
            master_availability_windows
        IN SHARE ROW EXCLUSIVE MODE
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _booking_redirect_map (
            source_master_id integer PRIMARY KEY,
            target_master_id integer NOT NULL
        ) ON COMMIT DROP
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _booking_redirect_chain ON COMMIT DROP AS
        WITH RECURSIVE redirect_chain AS (
            SELECT
                master.id AS source_master_id,
                master.id AS current_master_id,
                master.booking_redirect_master_id AS next_master_id,
                ARRAY[master.id]::integer[] AS path,
                false AS cycle
            FROM masters AS master
            WHERE master.booking_redirect_master_id IS NOT NULL

            UNION ALL

            SELECT
                chain.source_master_id,
                next_master.id,
                next_master.booking_redirect_master_id,
                chain.path || next_master.id,
                next_master.id = ANY(chain.path)
            FROM redirect_chain AS chain
            JOIN masters AS next_master ON next_master.id = chain.next_master_id
            WHERE chain.next_master_id IS NOT NULL
              AND NOT chain.cycle
        )
        SELECT * FROM redirect_chain
        """
    )
    op.execute(
        """
        INSERT INTO _booking_redirect_map (source_master_id, target_master_id)
        SELECT chain.source_master_id, chain.current_master_id
        FROM _booking_redirect_chain AS chain
        WHERE chain.next_master_id IS NULL
          AND NOT chain.cycle
        """
    )
    op.execute(
        """
        DO $$
        DECLARE invalid_source integer;
        BEGIN
            SELECT source.id
            INTO invalid_source
            FROM masters AS source
            LEFT JOIN _booking_redirect_map AS mapping
              ON mapping.source_master_id = source.id
            LEFT JOIN masters AS target
              ON target.id = mapping.target_master_id
            WHERE source.booking_redirect_master_id IS NOT NULL
              AND (mapping.source_master_id IS NULL OR target.is_active IS NOT TRUE)
            ORDER BY source.id
            LIMIT 1;

            IF invalid_source IS NOT NULL THEN
                RAISE EXCEPTION
                    'Cannot repair booking redirect for master %: chain is cyclic, broken, or ends at an inactive master',
                    invalid_source;
            END IF;

            SELECT chain.source_master_id
            INTO invalid_source
            FROM _booking_redirect_chain AS chain
            JOIN masters AS traversed_master
              ON traversed_master.id = chain.current_master_id
            WHERE traversed_master.is_active IS NOT TRUE
              AND chain.current_master_id <> chain.source_master_id
            ORDER BY chain.source_master_id, chain.current_master_id
            LIMIT 1;

            IF invalid_source IS NOT NULL THEN
                RAISE EXCEPTION
                    'Cannot repair booking redirect for master %: redirect chain contains an inactive master',
                    invalid_source;
            END IF;
        END $$
        """
    )

    # Future confirmed legacy bookings must move with their services.  The
    # migration aborts instead of guessing if a target service is absent or
    # ambiguous, if multiple source items collapse to one target item, or if
    # moving the booking would reveal an existing capacity conflict.
    op.execute(
        """
        CREATE TEMP TABLE _legacy_redirect_booking_services ON COMMIT DROP AS
        SELECT DISTINCT
            booking.id AS booking_id,
            booking.master_id AS source_master_id,
            mapping.target_master_id,
            selected.service_id AS old_service_id
        FROM bookings AS booking
        JOIN _booking_redirect_map AS mapping
          ON mapping.source_master_id = booking.master_id
        CROSS JOIN LATERAL (
            SELECT booking.service_id
            UNION
            SELECT item.service_id
            FROM booking_service_items AS item
            WHERE item.booking_id = booking.id
        ) AS selected
        WHERE booking.status = 'confirmed'
          AND booking.end_at > now()
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _legacy_redirect_service_candidates ON COMMIT DROP AS
        SELECT
            legacy.booking_id,
            legacy.source_master_id,
            legacy.target_master_id,
            legacy.old_service_id,
            target_service.id AS new_service_id,
            CASE
                WHEN target_service.id = source_service.id THEN 0
                WHEN source_service.base_service_id IS NOT NULL THEN 1
                ELSE 2
            END AS match_priority
        FROM _legacy_redirect_booking_services AS legacy
        JOIN barber_services AS source_service
          ON source_service.id = legacy.old_service_id
        JOIN barber_services AS target_service
          ON target_service.master_id = legacy.target_master_id
         AND target_service.is_active IS TRUE
         AND (
                target_service.id = source_service.id
                OR (
                    source_service.base_service_id IS NOT NULL
                    AND target_service.base_service_id = source_service.base_service_id
                )
                OR (
                    source_service.base_service_id IS NULL
                    AND target_service.base_service_id IS NULL
                    AND lower(btrim(COALESCE(target_service.title_uk, target_service.name)))
                        = lower(btrim(COALESCE(source_service.title_uk, source_service.name)))
                    AND lower(btrim(COALESCE(target_service.title_en, '')))
                        = lower(btrim(COALESCE(source_service.title_en, '')))
                    AND target_service.duration_minutes = source_service.duration_minutes
                    AND target_service.price = source_service.price
                )
         )
        """
    )
    op.execute(
        """
        DO $$
        DECLARE conflict_detail text;
        BEGIN
            SELECT format('booking %s service %s has no active target mapping', legacy.booking_id, legacy.old_service_id)
            INTO conflict_detail
            FROM _legacy_redirect_booking_services AS legacy
            LEFT JOIN _legacy_redirect_service_candidates AS candidate
              ON candidate.booking_id = legacy.booking_id
             AND candidate.old_service_id = legacy.old_service_id
            WHERE candidate.new_service_id IS NULL
            ORDER BY legacy.booking_id, legacy.old_service_id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;

            WITH best_priority AS (
                SELECT booking_id, old_service_id, min(match_priority) AS match_priority
                FROM _legacy_redirect_service_candidates
                GROUP BY booking_id, old_service_id
            )
            SELECT format('booking %s service %s has ambiguous target mappings', candidate.booking_id, candidate.old_service_id)
            INTO conflict_detail
            FROM _legacy_redirect_service_candidates AS candidate
            JOIN best_priority AS best
              ON best.booking_id = candidate.booking_id
             AND best.old_service_id = candidate.old_service_id
             AND best.match_priority = candidate.match_priority
            GROUP BY candidate.booking_id, candidate.old_service_id
            HAVING count(*) > 1
            ORDER BY candidate.booking_id, candidate.old_service_id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE TEMP TABLE _legacy_redirect_service_map ON COMMIT DROP AS
        SELECT DISTINCT ON (candidate.booking_id, candidate.old_service_id)
            candidate.booking_id,
            candidate.old_service_id,
            candidate.new_service_id
        FROM _legacy_redirect_service_candidates AS candidate
        ORDER BY
            candidate.booking_id,
            candidate.old_service_id,
            candidate.match_priority,
            candidate.new_service_id
        """
    )
    op.execute(
        """
        DO $$
        DECLARE conflict_detail text;
        BEGIN
            SELECT format('booking %s maps multiple selected services to service %s', booking_id, new_service_id)
            INTO conflict_detail
            FROM _legacy_redirect_service_map
            GROUP BY booking_id, new_service_id
            HAVING count(*) > 1
            ORDER BY booking_id, new_service_id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;

            WITH effective_bookings AS (
                SELECT
                    booking.id,
                    COALESCE(mapping.target_master_id, booking.master_id) AS target_master_id,
                    booking.start_at,
                    booking.end_at
                FROM bookings AS booking
                LEFT JOIN _booking_redirect_map AS mapping
                  ON mapping.source_master_id = booking.master_id
                WHERE booking.status = 'confirmed'
                  AND booking.end_at > now()
            ), moving AS (
                SELECT effective.*
                FROM effective_bookings AS effective
                JOIN bookings AS booking ON booking.id = effective.id
                JOIN _booking_redirect_map AS mapping
                  ON mapping.source_master_id = booking.master_id
            )
            SELECT format('booking %s overlaps booking %s on target master %s', moving.id, other.id, moving.target_master_id)
            INTO conflict_detail
            FROM moving
            JOIN effective_bookings AS other
              ON other.id <> moving.id
             AND other.target_master_id = moving.target_master_id
             AND other.start_at < moving.end_at
             AND other.end_at > moving.start_at
            ORDER BY moving.id, other.id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE booking_service_items AS item
        SET service_id = mapping.new_service_id,
            updated_at = now()
        FROM _legacy_redirect_service_map AS mapping
        WHERE mapping.booking_id = item.booking_id
          AND mapping.old_service_id = item.service_id
          AND mapping.new_service_id <> item.service_id
        """
    )
    op.execute(
        """
        UPDATE bookings AS booking
        SET service_id = service_mapping.new_service_id,
            redirected_from_master_id = COALESCE(booking.redirected_from_master_id, booking.master_id),
            master_id = redirect_mapping.target_master_id,
            updated_at = now()
        FROM _booking_redirect_map AS redirect_mapping
        JOIN _legacy_redirect_service_map AS service_mapping
          ON service_mapping.old_service_id IS NOT NULL
        WHERE redirect_mapping.source_master_id = booking.master_id
          AND service_mapping.booking_id = booking.id
          AND service_mapping.old_service_id = booking.service_id
          AND booking.status = 'confirmed'
          AND booking.end_at > now()
        """
    )

    # Open offers use the same physical calendar as bookings.  Pending offers
    # are ownership-only reservations until sent; sent/delivered offers are
    # active holds and must be conflict-free on the resolved target.
    op.execute(
        """
        DO $$
        DECLARE conflict_detail text;
        BEGIN
            WITH offers_after_repair AS (
                SELECT
                    offer.id,
                    offer.request_id,
                    CASE
                        WHEN offer.status IN ('pending', 'sent', 'delivered')
                             AND offer.end_at > now()
                        THEN COALESCE(mapping.target_master_id, offer.master_id)
                        ELSE offer.master_id
                    END AS master_id,
                    offer.start_at
                FROM waitlist_offers AS offer
                LEFT JOIN _booking_redirect_map AS mapping
                  ON mapping.source_master_id = offer.master_id
            )
            SELECT format(
                'waitlist offers for request %s collide on target master %s at %s',
                request_id,
                master_id,
                start_at
            )
            INTO conflict_detail
            FROM offers_after_repair
            GROUP BY request_id, master_id, start_at
            HAVING count(*) > 1
            ORDER BY request_id, master_id, start_at
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;

            WITH affected_holds AS (
                SELECT
                    offer.id,
                    COALESCE(mapping.target_master_id, offer.master_id) AS target_master_id,
                    offer.start_at,
                    offer.end_at
                FROM waitlist_offers AS offer
                LEFT JOIN _booking_redirect_map AS mapping
                  ON mapping.source_master_id = offer.master_id
                WHERE offer.status IN ('sent', 'delivered')
                  AND offer.end_at > now()
                  AND offer.expires_at > now()
                  AND (
                        mapping.source_master_id IS NOT NULL
                        OR offer.master_id IN (
                            SELECT target_master_id FROM _booking_redirect_map
                        )
                  )
            )
            SELECT format(
                'waitlist hold %s overlaps confirmed booking %s on target master %s',
                hold.id,
                booking.id,
                hold.target_master_id
            )
            INTO conflict_detail
            FROM affected_holds AS hold
            JOIN bookings AS booking
              ON booking.master_id = hold.target_master_id
             AND booking.status = 'confirmed'
             AND booking.start_at < hold.end_at
             AND booking.end_at > hold.start_at
            ORDER BY hold.id, booking.id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;

            WITH effective_holds AS (
                SELECT
                    offer.id,
                    COALESCE(mapping.target_master_id, offer.master_id) AS target_master_id,
                    offer.start_at,
                    offer.end_at,
                    mapping.source_master_id IS NOT NULL AS moving
                FROM waitlist_offers AS offer
                LEFT JOIN _booking_redirect_map AS mapping
                  ON mapping.source_master_id = offer.master_id
                WHERE offer.status IN ('sent', 'delivered')
                  AND offer.end_at > now()
                  AND offer.expires_at > now()
            )
            SELECT format(
                'waitlist hold %s overlaps waitlist hold %s on target master %s',
                moving.id,
                other.id,
                moving.target_master_id
            )
            INTO conflict_detail
            FROM effective_holds AS moving
            JOIN effective_holds AS other
              ON other.id <> moving.id
             AND other.target_master_id = moving.target_master_id
             AND other.start_at < moving.end_at
             AND other.end_at > moving.start_at
            WHERE moving.moving
            ORDER BY moving.id, other.id
            LIMIT 1;

            IF conflict_detail IS NOT NULL THEN
                RAISE EXCEPTION 'Redirect calendar repair aborted: %', conflict_detail;
            END IF;
        END $$
        """
    )
    op.execute(
        """
        UPDATE waitlist_offers AS offer
        SET source_master_id = COALESCE(offer.source_master_id, offer.master_id),
            master_id = mapping.target_master_id,
            updated_at = now()
        FROM _booking_redirect_map AS mapping
        WHERE mapping.source_master_id = offer.master_id
          AND offer.status IN ('pending', 'sent', 'delivered')
          AND offer.end_at > now()
        """
    )

    op.execute(
        """
        UPDATE master_time_blocks AS block
        SET master_id = mapping.target_master_id,
            updated_at = now()
        FROM _booking_redirect_map AS mapping
        WHERE mapping.source_master_id = block.master_id
          AND block.end_at > now()
        """
    )
    op.execute(
        """
        WITH duplicate_blocks AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY master_id, start_at, end_at, COALESCE(reason, '')
                       ORDER BY id
                   ) AS duplicate_number
            FROM master_time_blocks
            WHERE end_at > now()
              AND master_id IN (SELECT target_master_id FROM _booking_redirect_map)
        )
        DELETE FROM master_time_blocks AS block
        USING duplicate_blocks AS duplicate
        WHERE duplicate.id = block.id
          AND duplicate.duplicate_number > 1
        """
    )

    # Availability represents open capacity.  Unioning overlapping or adjacent
    # intervals preserves every open minute while restoring the application's
    # non-overlapping-window invariant on the terminal target calendar.
    op.execute(
        """
        CREATE TEMP TABLE _redirect_availability_merged ON COMMIT DROP AS
        WITH mapped AS (
            SELECT
                COALESCE(mapping.target_master_id, availability_window.master_id) AS master_id,
                availability_window.start_at,
                availability_window.end_at,
                availability_window.created_at,
                availability_window.updated_at
            FROM master_availability_windows AS availability_window
            LEFT JOIN _booking_redirect_map AS mapping
              ON mapping.source_master_id = availability_window.master_id
            WHERE availability_window.end_at > now()
              AND (
                    mapping.source_master_id IS NOT NULL
                    OR availability_window.master_id IN (SELECT target_master_id FROM _booking_redirect_map)
              )
        ), with_previous_end AS (
            SELECT mapped.*,
                   max(end_at) OVER (
                       PARTITION BY master_id
                       ORDER BY start_at, end_at
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS previous_max_end
            FROM mapped
        ), island_starts AS (
            SELECT with_previous_end.*,
                   CASE WHEN previous_max_end IS NULL OR previous_max_end < start_at THEN 1 ELSE 0 END AS starts_island
            FROM with_previous_end
        ), islands AS (
            SELECT island_starts.*,
                   sum(starts_island) OVER (
                       PARTITION BY master_id
                       ORDER BY start_at, end_at
                   ) AS island_id
            FROM island_starts
        )
        SELECT
            master_id,
            min(start_at) AS start_at,
            max(end_at) AS end_at,
            min(created_at) AS created_at,
            max(updated_at) AS updated_at
        FROM islands
        GROUP BY master_id, island_id
        """
    )
    op.execute(
        """
        DELETE FROM master_availability_windows AS availability_window
        WHERE availability_window.end_at > now()
          AND (
                availability_window.master_id IN (SELECT source_master_id FROM _booking_redirect_map)
                OR availability_window.master_id IN (SELECT target_master_id FROM _booking_redirect_map)
          )
        """
    )
    op.execute(
        """
        INSERT INTO master_availability_windows (
            master_id, start_at, end_at, created_at, updated_at
        )
        SELECT master_id, start_at, end_at, created_at, updated_at
        FROM _redirect_availability_merged
        ORDER BY master_id, start_at, end_at
        """
    )


def downgrade() -> None:
    # Data ownership repair is intentionally irreversible.  Reconstructing old
    # source calendars would be ambiguous and could create false availability.
    pass

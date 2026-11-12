import contextlib
import dataclasses
import datetime
import io
import logging
import os
import sys
import types
import unittest
from unittest import mock

import snscrape.base
import snscrape._cli as cli


UTC = datetime.timezone.utc


def dt(y, m, d, hour = 12):
	return datetime.datetime(y, m, d, hour, tzinfo = UTC)


@dataclasses.dataclass
class FakeItem(snscrape.base.Item):
	id: int
	date: datetime.datetime
	url: str = ''

	def __str__(self):
		return self.url or f'https://example.invalid/item/{self.id}'


@dataclasses.dataclass
class OtherItem(snscrape.base.Item):
	# Same numeric id space as FakeItem but a different data type; identities must not collide.
	id: int
	date: datetime.datetime

	def __str__(self):
		return f'https://other.invalid/{self.id}'


@dataclasses.dataclass
class UndatedItem(snscrape.base.Item):
	id: int
	date: object = None

	def __str__(self):
		return f'https://undated.invalid/{self.id}'


class FakeScraper(snscrape.base.Scraper):
	name = 'fake'

	def __init__(self, pages = None, itemError = None, pageError = None, entity = None):
		super().__init__()
		self._pages = list(pages or [])
		self._itemError = itemError
		self._pageError = pageError
		self._entity = entity

	def _get_entity(self):
		return self._entity

	def iter_item_pages(self):
		for i, page in enumerate(self._pages):
			if self._pageError is not None and i == self._pageError[0]:
				raise self._pageError[1]
			yield list(page)


class FailingPrint:
	'''A stdout stand-in that raises BrokenPipeError after `failAfter` successful writes.'''

	def __init__(self, failAfter):
		self.failAfter = failAfter
		self.written = []
		self._devnull = open(os.devnull, 'w')

	def write(self, s):
		if s == '\n':
			return 1
		if len(self.written) >= self.failAfter:
			raise BrokenPipeError('simulated broken pipe')
		self.written.append(s)
		return len(s)

	def flush(self):
		pass

	def fileno(self):
		# The BrokenPipeError handler redirects the real fd behind stdout to /dev/null.
		return self._devnull.fileno()


def makeArgs(**overrides):
	args = types.SimpleNamespace(
		withEntity = False,
		maxResults = None,
		jsonl = False,
		jsonlForBuggyIntParser = False,
		format = None,
		since = None,
		progress = False,
		retries = 3,
	)
	for k, v in overrides.items():
		setattr(args, k, v)
	return args


@contextlib.contextmanager
def captureStdout(stdout = None):
	if stdout is None:
		stdout = io.StringIO()
	with mock.patch.object(sys, 'stdout', stdout):
		yield stdout


class StableIdentityTest(unittest.TestCase):
	def test_identity_includes_type(self):
		a = FakeItem(1, dt(2024, 1, 1))
		b = OtherItem(1, dt(2024, 1, 1))
		self.assertNotEqual(snscrape.base.stable_item_identity(a), snscrape.base.stable_item_identity(b))

	def test_identity_stable_for_same_type_and_id(self):
		a = FakeItem(7, dt(2024, 1, 1))
		b = FakeItem(7, dt(2025, 1, 1))
		self.assertEqual(snscrape.base.stable_item_identity(a), snscrape.base.stable_item_identity(b))

	def test_identity_falls_back_to_str_without_id(self):
		@dataclasses.dataclass
		class NoIdItem(snscrape.base.Item):
			url: str

			def __str__(self):
				return self.url

		self.assertEqual(
			snscrape.base.stable_item_identity(NoIdItem('https://x/1')),
			snscrape.base.stable_item_identity(NoIdItem('https://x/1')),
		)
		self.assertNotEqual(
			snscrape.base.stable_item_identity(NoIdItem('https://x/1')),
			snscrape.base.stable_item_identity(NoIdItem('https://x/2')),
		)


class BaseWrapperTest(unittest.TestCase):
	def test_default_iter_item_pages_wraps_get_items_in_one_page(self):
		class FlatScraper(snscrape.base.Scraper):
			name = 'flat'

			def get_items(self):
				yield FakeItem(1, dt(2024, 1, 1))
				yield FakeItem(2, dt(2024, 1, 2))

		pages = list(FlatScraper().iter_item_pages())
		self.assertEqual(len(pages), 1)
		self.assertEqual([item.id for item in pages[0]], [1, 2])


class PinnedAndSinceTest(unittest.TestCase):
	def test_pinned_old_item_does_not_trigger_early_exit(self):
		# First page starts with a pinned item older than --since, followed by current items.
		# Old behaviour broke immediately; the new one must continue until a whole page is old.
		pages = [
			[FakeItem(1, dt(2020, 1, 1)), FakeItem(10, dt(2024, 5, 1)), FakeItem(9, dt(2024, 4, 1))],
			[FakeItem(8, dt(2024, 3, 1)), FakeItem(7, dt(2024, 2, 1))],
			[FakeItem(6, dt(2023, 1, 1)), FakeItem(5, dt(2023, 1, 2))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(since = dt(2024, 1, 1)), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/item/10',
			'https://example.invalid/item/9',
			'https://example.invalid/item/8',
			'https://example.invalid/item/7',
		])

	def test_out_of_order_dates_within_page_keep_stream_going(self):
		# A page whose first item is old but which also contains a new item must not count as an all-old page.
		pages = [
			[FakeItem(3, dt(2023, 1, 1)), FakeItem(4, dt(2024, 6, 1))],
			[FakeItem(2, dt(2023, 2, 1)), FakeItem(1, dt(2023, 3, 1))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(since = dt(2024, 1, 1)), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/item/4'])

	def test_cursor_exhausted_when_no_page_is_fully_old(self):
		# Without --since, all pages are consumed to the end even if dates are not ordered.
		pages = [
			[FakeItem(1, dt(2024, 1, 1))],
			[FakeItem(2, dt(2020, 1, 1)), FakeItem(3, dt(2025, 1, 1))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(len(out.getvalue().splitlines()), 3)

	def test_since_boundary_is_exclusive_for_old_and_inclusive_for_equal(self):
		cutoff = dt(2024, 1, 1)
		pages = [[FakeItem(1, cutoff), FakeItem(2, dt(2023, 12, 31))]]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(since = cutoff), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/item/1'])

	def test_empty_pages_are_skipped_without_terminating(self):
		# An empty page must never be treated as an all-old page.
		pages = [
			[],
			[FakeItem(1, dt(2024, 1, 1))],
			[],
			[FakeItem(2, dt(2023, 1, 1)), FakeItem(3, dt(2023, 1, 2))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(since = dt(2024, 1, 1)), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/item/1'])


class OverlapDedupTest(unittest.TestCase):
	def test_overlapping_pages_deduplicated_by_identity(self):
		pages = [
			[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))],
			[FakeItem(2, dt(2024, 1, 2)), FakeItem(3, dt(2024, 1, 3))],
			[FakeItem(3, dt(2024, 1, 3))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
			'https://example.invalid/item/3',
		])

	def test_duplicates_do_not_consume_max_results_budget(self):
		pages = [
			[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))],
			[FakeItem(2, dt(2024, 1, 2)), FakeItem(3, dt(2024, 1, 3))],
			[FakeItem(4, dt(2024, 1, 4))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(maxResults = 3), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
			'https://example.invalid/item/3',
		])

	def test_same_id_different_types_both_emitted(self):
		pages = [[FakeItem(5, dt(2024, 1, 1)), OtherItem(5, dt(2024, 1, 1))]]
		scraper = FakeScraper(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(len(out.getvalue().splitlines()), 2)


class MissingFieldsTest(unittest.TestCase):
	def test_item_without_date_is_emitted_and_does_not_stop_since(self):
		pages = [
			[UndatedItem(1), FakeItem(10, dt(2024, 5, 1))],
			[FakeItem(9, dt(2024, 4, 1)), FakeItem(8, dt(2024, 3, 1))],
			[FakeItem(7, dt(2023, 1, 1)), FakeItem(6, dt(2023, 1, 2))],
		]
		scraper = FakeScraper(pages)
		with captureStdout() as out, self.assertLogs('snscrape._cli', level = logging.WARNING) as logs:
			rc = cli.run(makeArgs(since = dt(2024, 1, 1)), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		lines = out.getvalue().splitlines()
		self.assertIn('https://undated.invalid/1', lines)
		self.assertIn('https://example.invalid/item/10', lines)
		self.assertIn('https://example.invalid/item/9', lines)
		self.assertIn('https://example.invalid/item/8', lines)
		# The all-old final page must not terminate the run early while an undatable item exists earlier;
		# the run ends via cursor exhaustion instead, and old items are filtered out.
		self.assertNotIn('https://example.invalid/item/7', lines)
		self.assertTrue(any('no usable date' in msg for msg in logs.output))


class BadCursorTest(unittest.TestCase):
	def test_failure_on_second_page_returns_scraper_error_and_keeps_committed_lines(self):
		pages = [
			[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))],
			[FakeItem(3, dt(2024, 1, 3))],
		]
		scraper = FakeScraper(pages, pageError = (1, snscrape.base.ScraperException('bad cursor')))
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_SCRAPER_ERROR)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
		])

	def test_parse_failure_inside_page_distinguishable_from_broken_pipe(self):
		pages = [
			[FakeItem(1, dt(2024, 1, 1))],
			[FakeItem(2, dt(2024, 1, 2))],
		]
		scraper = FakeScraper(pages, pageError = (1, ValueError('malformed response payload')))
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_SCRAPER_ERROR)
		self.assertNotEqual(rc, cli.EXIT_BROKEN_PIPE)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/item/1'])

	def test_bad_cursor_with_since_keeps_committed_new_lines_only(self):
		# The first page contains an old pinned item; new items are committed. The failing
		# second page must surface the scraper error code while keeping the committed lines.
		pages = [
			[FakeItem(1, dt(2020, 1, 1)), FakeItem(10, dt(2024, 5, 1))],
			[FakeItem(11, dt(2024, 6, 1))],
		]
		scraper = FakeScraper(pages, pageError = (1, snscrape.base.ScraperException('cursor rejected')))
		with captureStdout() as out:
			rc = cli.run(makeArgs(since = dt(2024, 1, 1)), scraper)
		self.assertEqual(rc, cli.EXIT_SCRAPER_ERROR)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/item/10'])

	def test_scraper_construction_failure_uses_scraper_error_code(self):
		class FailingScraper(FakeScraper):
			def __init__(self, *args, **kwargs):
				raise snscrape.base.EntityUnavailable('entity gone')

		with captureStdout() as out:
			rc = cli.run(makeArgs(cls = FailingScraper))
		self.assertEqual(rc, cli.EXIT_SCRAPER_ERROR)
		self.assertEqual(out.getvalue(), '')


class BrokenPipeTest(unittest.TestCase):
	def test_broken_pipe_keeps_committed_lines_and_uses_dedicated_exit_code(self):
		pages = [
			[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))],
			[FakeItem(3, dt(2024, 1, 3)), FakeItem(4, dt(2024, 1, 4))],
		]
		scraper = FakeScraper(pages)
		failingOut = FailingPrint(failAfter = 2)
		with mock.patch.object(sys, 'stdout', failingOut):
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_BROKEN_PIPE)
		self.assertEqual(failingOut.written, [
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
		])

	def test_count_only_increments_after_successful_write(self):
		pages = [[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))]]
		scraper = FakeScraper(pages)
		failingOut = FailingPrint(failAfter = 1)
		with mock.patch.object(sys, 'stdout', failingOut):
			rc = cli.run(makeArgs(maxResults = 2), scraper)
		self.assertEqual(rc, cli.EXIT_BROKEN_PIPE)
		self.assertEqual(len(failingOut.written), 1)


class MaxResultsAndEntityTest(unittest.TestCase):
	def test_entity_emitted_first_and_not_counted(self):
		entity = FakeItem(999, dt(2024, 1, 1), url = 'https://example.invalid/entity')
		pages = [[FakeItem(i, dt(2024, 1, i)) for i in range(1, 4)]]
		scraper = FakeScraper(pages, entity = entity)
		with captureStdout() as out:
			rc = cli.run(makeArgs(withEntity = True, maxResults = 2), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/entity',
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
		])

	def test_max_results_zero_with_entity(self):
		entity = FakeItem(999, dt(2024, 1, 1), url = 'https://example.invalid/entity')
		scraper = FakeScraper([[FakeItem(1, dt(2024, 1, 1))]], entity = entity)
		with captureStdout() as out:
			rc = cli.run(makeArgs(withEntity = True, maxResults = 0), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), ['https://example.invalid/entity'])

	def test_jsonl_output_is_one_object_per_line(self):
		pages = [[FakeItem(1, dt(2024, 1, 1)), FakeItem(2, dt(2024, 1, 2))]]
		scraper = FakeScraper(pages)
		import json
		with captureStdout() as out:
			rc = cli.run(makeArgs(jsonl = True), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		lines = out.getvalue().splitlines()
		parsed = [json.loads(line) for line in lines]
		self.assertEqual([obj['id'] for obj in parsed], [1, 2])
		self.assertTrue(all(obj['_type'].endswith('FakeItem') for obj in parsed))


class FormatAndDefaultOutputTest(unittest.TestCase):
	class _PagedFake(FakeScraper):
		def get_items(self):
			# Mirrors the pattern used by every real scraper: get_items() stays a flat iterator over iter_item_pages().
			for page in self.iter_item_pages():
				yield from page

	def test_library_iterator_flattening_pattern(self):
		pages = [[FakeItem(1, dt(2024, 1, 1))], [FakeItem(2, dt(2024, 1, 2)), FakeItem(3, dt(2024, 1, 3))]]
		scraper = self._PagedFake(pages)
		self.assertEqual([item.id for item in scraper.get_items()], [1, 2, 3])
		with captureStdout() as out:
			rc = cli.run(makeArgs(), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().splitlines(), [
			'https://example.invalid/item/1',
			'https://example.invalid/item/2',
			'https://example.invalid/item/3',
		])

	def test_format_string_output(self):
		pages = [[FakeItem(7, dt(2024, 1, 1), url = 'u7')]]
		scraper = self._PagedFake(pages)
		with captureStdout() as out:
			rc = cli.run(makeArgs(format = 'id={0.id}'), scraper)
		self.assertEqual(rc, cli.EXIT_OK)
		self.assertEqual(out.getvalue().strip(), 'id=7')


if __name__ == '__main__':
	unittest.main()

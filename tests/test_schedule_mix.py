import unittest
from collections import Counter
from itertools import groupby

from podcast_agent.schedule import interleave_sources


class ScheduleMixTests(unittest.TestCase):
    def test_unequal_source_counts_are_spread_out_without_a_long_tail(self):
        records = [{'clip_id': f'{source}-{i}', 'video': {'id': source}}
                   for source, count in [('podcast-a', 9), ('podcast-b', 5)] for i in range(count)]
        mixed = interleave_sources(records)
        sources = [r['video']['id'] for r in mixed]
        self.assertEqual(Counter(r['clip_id'] for r in mixed), Counter(r['clip_id'] for r in records))
        self.assertLessEqual(max(len(list(group)) for _, group in groupby(sources)), 2)
        self.assertEqual(mixed, interleave_sources(records))

    def test_balanced_sources_alternate_and_empty_input_is_valid(self):
        records = [{'clip_id': f'{source}-{i}', 'video': {'id': source}}
                   for source in ('a', 'b') for i in range(3)]
        self.assertEqual([r['video']['id'] for r in interleave_sources(records)], ['a','b','a','b','a','b'])
        self.assertEqual(interleave_sources([]), [])


if __name__ == '__main__':
    unittest.main()

class FreshQueueMixTests(unittest.TestCase):
    def test_new_source_enters_early_and_is_spread_through_existing_backlog(self):
        import random
        from podcast_agent.schedule import mix_sources
        records=[{'id':f'{source}-{i}','video':{'id':source}} for source,count in [('old-a',9),('old-b',9),('new',5)] for i in range(count)]
        mixed=mix_sources(records,fresh_ids=[r['id'] for r in records if r['video']['id']=='new'],rng=random.Random(4))
        self.assertEqual(Counter(r['id'] for r in records),Counter(r['id'] for r in mixed))
        self.assertEqual(mixed[0]['video']['id'],'new')
        sources=[r['video']['id'] for r in mixed]
        self.assertLessEqual(max(len(list(g)) for _,g in groupby(sources)),2)
        self.assertGreater(sources.index('new',1),1)
        self.assertNotEqual([r['id'] for r in mixed],[r['id'] for r in mix_sources(records,fresh_ids=['new-0'],rng=random.Random(5))])

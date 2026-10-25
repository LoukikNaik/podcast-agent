import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from podcast_agent.instagram import caption_for


class CaptionStyleTests(unittest.TestCase):
    def test_captions_and_source_credit_do_not_contain_em_dashes(self):
        with patch('podcast_agent.instagram.decide', return_value={
                'caption': 'One question — two perspectives.', 'hashtags': ['mindset', 'podcast']}):
            caption = caption_for(SimpleNamespace(), Path('/tmp'), 'clip',
                                  {'title': 'Two Honest Perspectives', 'start': 10},
                                  {'id': 'abcdefghijk', 'title': 'A conversation', 'channel': 'Podcast — Official'},
                                  'One question, two perspectives.')
        self.assertNotIn('—', caption)
        self.assertIn('One question, two perspectives.', caption)
        self.assertIn('Source: Podcast, Official', caption)
        self.assertIn('https://www.youtube.com/watch?v=abcdefghijk&t=10s', caption)


if __name__ == '__main__':
    unittest.main()

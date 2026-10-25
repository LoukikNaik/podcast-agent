import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from podcast_agent import instagram, instagram_client
from podcast_agent.pipeline import database


class InstagramActionTests(unittest.TestCase):
    def setUp(self):
        self.info = SimpleNamespace(pk='42', username='owner')
        self.media = {'id': '123_42', 'pk': '123', 'user': {'pk': '42'}, 'has_liked': False,
                      'media_notes': {'items': []}}
        self.client = SimpleNamespace(uuid='device', private_request=Mock(return_value={'items': [self.media]}),
                                      media_like=Mock(return_value=True), media_note_create=Mock(return_value={'status': 'ok', 'id': '987', 'media_id': '123'}))

    def test_like_checks_ownership_then_likes_the_published_media(self):
        result = instagram_client.engage(self.client, self.info, '123', 'like')
        self.assertEqual(result['state'], 'done')
        self.client.media_like.assert_called_once_with('123_42')

    def test_existing_own_native_repost_is_not_created_again(self):
        self.media['media_notes']['items'] = [{'id': '987', 'user_id': '42', 'media_id': '123',
                                              'audience': 7, 'note_style': 13}]
        result = instagram_client.engage(self.client, self.info, '123', 'repost')
        self.assertEqual(result['repost_id'], '987')
        self.assertEqual(result['state'], 'done')
        self.assertEqual(self.client.private_request.call_count, 1)
        self.client.media_note_create.assert_not_called()

    def test_repost_uses_the_existing_instagrapi_method(self):
        result = instagram_client.engage(self.client, self.info, '123', 'repost')
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['repost_id'], '987')
        self.client.media_note_create.assert_called_once_with('123_42')

    def test_timeout_during_repost_is_uncertain_and_not_repeated(self):
        self.client.media_note_create.side_effect = TimeoutError('secret must not leak')
        result = instagram_client.engage(self.client, self.info, '123', 'repost')
        self.assertEqual(result['state'], 'uncertain')
        self.assertNotIn('secret', str(result))
        self.assertEqual(self.client.media_note_create.call_count, 1)

    def test_reconciliation_never_sends_a_missing_repost_again(self):
        result = instagram_client.engage(self.client, self.info, '123', 'repost', perform=False)
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(self.client.private_request.call_count, 1)
        self.client.media_note_create.assert_not_called()

    def test_preflight_failure_can_be_retried_without_mutating(self):
        self.client.private_request.side_effect = TimeoutError()
        self.assertEqual(instagram_client.engage(self.client, self.info, '123', 'repost')['state'], 'retry')
        self.client.media_like.assert_not_called()

    def test_instagrapi_does_not_resubmit_a_timed_out_repost_internally(self):
        from instagrapi import Client
        from instagrapi.exceptions import ClientRequestTimeout
        client = Client()
        client.authorization_data = {'ds_user_id': '42'}
        client.request_timeout = 0
        client.delay_range = None
        def send(endpoint, *args, **kwargs):
            if endpoint.endswith('/info/'):
                client.last_json = {'items': [self.media]}
                return client.last_json
            raise ClientRequestTimeout()
        with patch.object(client, '_send_private_request', side_effect=send) as request, patch('instagrapi.mixins.private.time.sleep'):
            result = instagram_client.engage(client, self.info, '123', 'repost')
        self.assertEqual(result['state'], 'uncertain')
        self.assertEqual(request.call_count, 2)

    def test_followups_cannot_touch_another_accounts_media(self):
        self.media['user']['pk'] = '99'
        self.assertEqual(instagram_client.engage(self.client, self.info, '123', 'like')['state'], 'failed')
        self.client.media_like.assert_not_called()


class PostingJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = SimpleNamespace(data=Path(self.temp.name), secrets={'INSTAGRAM_USER': 'owner'})
        self.directory = self.ctx.data / 'post'
        self.directory.mkdir()
        self.saved = self.ctx.data / 'one.mp4'
        self.saved.touch()

    def test_followup_failure_keeps_the_publication_receipt_and_never_uploads_again(self):
        with database(self.ctx) as db:
            def run(*args, **kwargs):
                (self.directory / 'instagram-cover.jpg').touch()
            receipt = {'pk': '123', 'url': 'https://www.instagram.com/reel/abc/'}
            with patch.object(instagram, 'run', side_effect=run), patch.object(instagram, 'worker', return_value=receipt) as worker, patch.object(instagram, 'executable', return_value='ffmpeg'):
                instagram.post(self.ctx, db, 'one', self.saved, 'Caption', self.directory)
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(db.execute('SELECT state FROM instagram_posts').fetchone()[0], 'posted')
            with patch.object(instagram, 'worker', side_effect=TimeoutError()):
                instagram.finish_actions(self.ctx, db)
            self.assertEqual(db.execute('SELECT state FROM instagram_posts').fetchone()[0], 'posted')
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_actions WHERE state="uncertain"').fetchone()[0], 2)
            with patch.object(instagram, 'worker') as worker:
                instagram.post(self.ctx, db, 'one', self.saved, 'Caption', self.directory)
            worker.assert_not_called()

    def test_a_crashed_action_is_reconciled_without_repeating_its_mutation(self):
        with database(self.ctx) as db:
            db.execute("INSERT INTO instagram_posts (id,account,state,media_pk) VALUES ('owner:one','owner','posted','123')")
            db.execute("INSERT INTO instagram_actions (post_id,action,state) VALUES ('owner:one','repost','sending')")
            db.commit()
            with patch.object(instagram, 'worker', return_value={'username': 'owner', 'media_pk': '123', 'action': 'repost', 'state': 'done', 'repost_id': '987'}) as worker:
                instagram.finish_actions(self.ctx, db)
            self.assertFalse(worker.call_args.args[2]['perform'])
            self.assertEqual(db.execute('SELECT state FROM instagram_actions').fetchone()[0], 'done')

class RescheduleTests(unittest.TestCase):
    def test_rescheduling_only_moves_queued_reels_into_future_slots(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)
            ctx = SimpleNamespace(data=Path(folder), secrets={'INSTAGRAM_USER': 'owner'},
                cfg={'instagram_times': ['10:00', '12:00', '14:00', '16:00', '18:00'], 'clips_per_day': 5},
                now=lambda: now,
                slot=lambda day, value: datetime.combine(day, datetime.strptime(value, '%H:%M').time(), tzinfo=timezone.utc))
            with database(ctx) as db:
                db.execute("INSERT INTO instagram_posts (id,account,state,posted_at,scheduled_at) VALUES ('posted','owner','posted',100,200)")
                for i in range(3):
                    db.execute("INSERT INTO instagram_posts (id,account,clip_id,state,queued_at) VALUES (?,?,?,'queued',?)", (f'queued-{i}', 'owner', f'clip-{i}', i))
                slots = instagram.reschedule_posts(ctx, db, mix=False)
                self.assertEqual([r['id'] for r in slots], ['queued-0', 'queued-1', 'queued-2'])
                self.assertTrue(all(r['scheduled_at'] > now.timestamp() for r in slots))
                self.assertEqual([datetime.fromtimestamp(r['scheduled_at'], timezone.utc).isoformat() for r in slots], ['2026-10-25T16:00:00+00:00','2026-10-25T18:00:00+00:00','2026-10-26T10:00:00+00:00'])
                self.assertEqual(db.execute("SELECT state,posted_at,scheduled_at FROM instagram_posts WHERE id='posted'").fetchone(), ('posted', 100, 200))

class PublisherIntegrationTests(unittest.TestCase):
    def test_publisher_finishes_actions_even_when_no_upload_slot_is_due(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026,10,25,15,0,tzinfo=timezone.utc)
            ctx = SimpleNamespace(data=Path(folder), secrets={'INSTAGRAM_USER':'owner'},
                cfg={'instagram':True,'clips_per_day':12,'instagram_times':['16:00']},
                now=lambda:now,
                slot=lambda day,value:datetime.combine(day,datetime.strptime(value,'%H:%M').time(),tzinfo=timezone.utc))
            with database(ctx) as db:
                db.execute("INSERT INTO instagram_posts (id,account,state,media_pk) VALUES ('owner:one','owner','posted','123')")
                db.execute("INSERT INTO instagram_actions (post_id,action) VALUES ('owner:one','like')")
                db.commit()
            with patch.object(instagram,'require_instagram'),patch.object(instagram,'worker',return_value={'username':'owner','media_pk':'123','action':'like','state':'done'}) as worker:
                instagram.publish_one(ctx,scheduled=True)
            self.assertEqual(worker.call_args.args[1],'engage')
            with database(ctx) as db:
                self.assertEqual(db.execute('SELECT state FROM instagram_actions').fetchone()[0],'done')

    def test_future_rescheduled_reel_is_not_uploaded_early(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder:
            now = datetime(2026,10,25,15,0,tzinfo=timezone.utc)
            ctx = SimpleNamespace(data=Path(folder), secrets={'INSTAGRAM_USER':'owner'},
                cfg={'instagram':True,'clips_per_day':12},now=lambda:now,
                slot=lambda day,value:datetime.combine(day,datetime.strptime(value,'%H:%M').time(),tzinfo=timezone.utc))
            with database(ctx) as db:
                db.execute("INSERT INTO instagram_posts (id,account,state,scheduled_at) VALUES ('owner:one','owner','queued',?)",(now.timestamp()+3600,))
                db.commit()
            with patch.object(instagram,'require_instagram'),patch.object(instagram,'worker') as worker:
                instagram.publish_one(ctx)
            worker.assert_not_called()

class QueueArrivalTests(unittest.TestCase):
    def test_new_arrival_gets_next_slot_and_existing_posts_stay_unchanged(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as folder:
            now=datetime(2026,10,25,15,0,tzinfo=timezone.utc)
            ctx=SimpleNamespace(data=Path(folder),secrets={'INSTAGRAM_USER':'owner'},
                cfg={'clips_per_day':12,'instagram_times':['16:00','18:00','20:00']},now=lambda:now,
                slot=lambda day,value:datetime.combine(day,datetime.strptime(value,'%H:%M').time(),tzinfo=timezone.utc))
            with database(ctx) as db:
                db.execute("INSERT INTO instagram_posts (id,account,state,posted_at,scheduled_at) VALUES ('posted','owner','posted',100,200)")
                for i in range(4):
                    db.execute("INSERT INTO instagram_posts (id,account,clip_id,state,queued_at,path) VALUES (?,?,?,'queued',?,?)",(f'old-{i}','owner',f'old-{i}',i,'missing.mp4'))
                instagram.enqueue(ctx,db,'fresh',Path(folder)/'fresh.mp4','Fresh caption')
                rows=db.execute("SELECT id FROM instagram_posts WHERE state='queued' ORDER BY scheduled_at").fetchall()
                self.assertEqual(rows[0][0],'owner:fresh')
                self.assertEqual(len(rows),5)
                self.assertEqual(db.execute("SELECT state,posted_at,scheduled_at FROM instagram_posts WHERE id='posted'").fetchone(),('posted',100,200))
                before=db.execute("SELECT id,scheduled_at FROM instagram_posts ORDER BY id").fetchall()
                instagram.enqueue(ctx,db,'fresh',Path(folder)/'fresh.mp4','Updated caption')
                self.assertEqual(before,db.execute("SELECT id,scheduled_at FROM instagram_posts ORDER BY id").fetchall())


class SessionUpgradeTests(unittest.TestCase):
    def test_upgrade_keeps_the_session_and_device_but_uses_a_supported_app_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            ctx = SimpleNamespace(data=Path(folder),secrets={'INSTAGRAM_USER':'owner'})
            path=ctx.data/'instagram/session.json';path.parent.mkdir();path.write_text('{}')
            client=Mock();client.account_info.return_value=SimpleNamespace(username='owner')
            client.get_settings.return_value={}
            with patch('instagrapi.Client',return_value=client):
                instagram_client.connect(ctx)
            client.load_settings.assert_called_once_with(path,override_app_version=True)
            client.login.assert_not_called()


if __name__ == '__main__':
    unittest.main()

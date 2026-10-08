"""A raw-return bridge is allowed only after complete official checks."""

from datetime import datetime, timezone

import pytest
import requests

from pipeline.freshness import FreshnessError
from pipeline.verified_bridge import AUDITED_CNINFO_NOTICES, verify_no_actions


NOW = datetime(2026, 10, 8, 23, tzinfo=timezone.utc)  # Oct 9 Beijing


class Reply:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def responses(code='161130'):
    report = {
        '161130': (811201, '易方达纳斯达克100交易型开放式指数证券投资基金联接基金（LOF）2026年中期报告'),
        '161125': (811196, '易方达标普500指数证券投资基金（LOF）2026年中期报告'),
    }[code]
    suspension = '关于旗下部分基金2026年9月7日暂停申购、赎回、转换、定期定额投资业务的提示性公告'
    cninfo_control_pdf = {'161130': '1225541045', '161125': '1225541050'}[code]
    return [
        {'status': 1, 'total': 0, 'data': []},
        {'status': 1, 'data': {'total': 2, 'data': [
            {'id': 813013, 'title': suspension},
            {'id': report[0], 'title': report[1]},
        ]}},
        {'status': 1, 'data': {'total': 0, 'data': []}},
        {'totalAnnouncement': 1, 'totalRecordNum': 1,
         'hasMore': False, 'announcements': [{
             'secCode': code, 'announcementTitle': suspension,
             'announcementTime': 1788278400000,
             'adjunctUrl': f'finalpage/2026-09-02/{cninfo_control_pdf}.PDF',
         }]},
        {'totalAnnouncement': 1, 'totalRecordNum': 1,
         'hasMore': False, 'announcements': [AUDITED_CNINFO_NOTICES[code].copy()]},
    ]


def transport(payloads):
    calls = []
    remaining = list(payloads)

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Reply(remaining.pop(0))

    return post, calls


@pytest.mark.parametrize('code,pdf_id', [('161130', '1225577651'),
                                         ('161125', '1225577646')])
def test_verified_no_action_window_and_provenance(code, pdf_id):
    post, calls = transport(responses(code))
    proof = verify_no_actions(code, '2026-10-08', now=NOW, post=post)
    assert proof['fund_code'] == code
    assert proof['market_through'] == '2026-10-08'
    assert proof['notices_from'] == '2026-09-03'
    assert proof['notices_through'] == '2026-10-09'  # also sees pre-open notices
    assert proof['checked_at'] == '2026-10-08T23:00:00+00:00'
    assert proof['audited_non_action_notices'] == [
        f'finalpage/2026-09-23/{pdf_id}.PDF']
    assert len(proof['sources']) == 3 and len(calls) == 5
    assert calls[0][1]['json']['startDate'] == calls[0][1]['json']['endDate'] == ''
    assert calls[0][1]['json']['fundCode'] == code
    assert calls[1][1]['json']['prop1'] == '2026-08-31,2026-09-03'
    assert calls[2][1]['json']['prop1'] == '2026-09-03,2026-10-09'
    assert calls[2][1]['json']['fundCode'] == code
    assert calls[3][1]['data']['seDate'] == '2026-09-02~2026-09-02'
    assert calls[4][1]['data']['seDate'] == '2026-09-03~2026-10-09'
    assert calls[4][1]['data']['stock'] == f'{code},jjjl0000041'


@pytest.mark.parametrize('which,replacement', [
    (0, {'status': 1, 'total': 1, 'data': [{}]}),
    (2, {'status': 1, 'data': {'total': 1, 'data': [{}]}}),
    (4, {'totalAnnouncement': 1, 'totalRecordNum': 1,
         'hasMore': False, 'announcements': [{}]}),
])
def test_any_new_bonus_or_notice_blocks_bridge(which, replacement):
    payloads = responses()
    payloads[which] = replacement
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='161130'):
        verify_no_actions('161130', '2026-10-08', now=NOW, post=post)


def test_cninfo_known_notice_only_allows_exact_audited_record():
    payloads = responses()
    payloads[4]['announcements'][0]['announcementTime'] += 1
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='unaudited notice'):
        verify_no_actions('161130', '2026-10-08', now=NOW, post=post)

    payloads = responses()
    payloads[4] = {'totalAnnouncement': 2, 'totalRecordNum': 2,
                   'hasMore': False,
                   'announcements': [AUDITED_CNINFO_NOTICES['161130'].copy(), {}]}
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='unaudited notice'):
        verify_no_actions('161130', '2026-10-08', now=NOW, post=post)


def test_controls_reject_wrong_fund_identity():
    payloads = responses('161125')
    payloads[1]['data']['data'][1]['id'] = 811201  # 161130 report
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='161125.*positive control'):
        verify_no_actions('161125', '2026-10-08', now=NOW, post=post)

    payloads = responses('161125')
    payloads[3]['announcements'][0]['secCode'] = '161130'
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='161125.*positive control'):
        verify_no_actions('161125', '2026-10-08', now=NOW, post=post)

def test_161125_rejects_161130_notice_id():
    payloads = responses('161125')
    payloads[4]['announcements'] = [AUDITED_CNINFO_NOTICES['161130'].copy()]
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='161125.*unaudited notice'):
        verify_no_actions('161125', '2026-10-08', now=NOW, post=post)


@pytest.mark.parametrize('which,replacement', [
    (1, {'status': 1, 'data': {'total': 0, 'data': []}}),  # lost known notice
    (2, {'status': 1, 'data': {'total': 0}}),  # missing rows
    (2, {'status': 1, 'data': {'total': 101, 'data': []}}),  # truncated page
    (4, {'totalAnnouncement': 0, 'totalRecordNum': 0,
         'hasMore': True, 'announcements': None}),
])
def test_missing_control_or_incomplete_response_blocks_bridge(which, replacement):
    payloads = responses()
    payloads[which] = replacement
    post, _ = transport(payloads)
    with pytest.raises(FreshnessError, match='161130'):
        verify_no_actions('161130', '2026-10-08', now=NOW, post=post)


def test_official_api_failure_blocks_bridge():
    def post(*args, **kwargs):
        raise requests.Timeout('timed out')

    with pytest.raises(FreshnessError, match='E Fund bonus disclosure query failed'):
        verify_no_actions('161130', '2026-10-08', now=NOW, post=post)


@pytest.mark.parametrize('code,day', [('161126', '2026-10-08'),
                                      ('161130', '2026-09-29'),
                                      ('161130', '2026-10-10'),
                                      ('161125', '2026-09-29')])
def test_wrong_symbol_or_date_blocks_before_network(code, day):
    with pytest.raises(FreshnessError):
        verify_no_actions(code, day, now=NOW,
                          post=lambda *a, **k: pytest.fail('unexpected network call'))

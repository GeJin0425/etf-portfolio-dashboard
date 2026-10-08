"""Official-disclosure guard for *conditional* E Fund LOF raw-return bridges.

This does not convert raw quotes to adjusted quotes. Callers may extend the
frozen adjusted close only after this guard confirms that the known official
disclosure feeds contain no new notice through the build date.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .freshness import FreshnessError


EFUNDS_BONUS = 'https://api.efunds.com.cn/xcowch/front/fundShareBonus/list'
EFUNDS_NOTICES = 'https://api.efunds.com.cn/xcowch/front/contents'
CNINFO_NOTICES = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
NOTICE_START = date(2026, 9, 3)
ANCHOR = date(2026, 9, 30)
PAGE_SIZE = 100
UA = {'User-Agent': 'Mozilla/5.0'}
# Both code-specific CNINFO PDFs have identical SHA-256
# 64e29f50225025d86fc779ffba85c33ac32f26745b0382b82d879573717fe377.
# They concern retail direct-sales account migration only: no distribution,
# share conversion, split, or ex-date for either fund.
_NON_ACTION_TITLE = '易方达基金管理有限公司及易方达财富管理基金销售（广州）有限公司关于零售直销业务迁移安排的联合提示性公告'
AUDITED_CNINFO_NOTICES = {
    code: {
        'secCode': code,
        'announcementTitle': _NON_ACTION_TITLE,
        'announcementTime': 1790092800000,
        'adjunctUrl': f'finalpage/2026-09-23/{pdf_id}.PDF',
    }
    for code, pdf_id in [('161130', '1225577651'), ('161125', '1225577646')]
}
_SUSPENSION_TITLE = '关于旗下部分基金2026年9月7日暂停申购、赎回、转换、定期定额投资业务的提示性公告'
EFUNDS_CONTROL_REPORTS = {
    '161130': (811201, '易方达纳斯达克100交易型开放式指数证券投资基金联接基金（LOF）2026年中期报告'),
    '161125': (811196, '易方达标普500指数证券投资基金（LOF）2026年中期报告'),
}
CNINFO_CONTROL_NOTICES = {
    code: {
        'secCode': code,
        'announcementTitle': _SUSPENSION_TITLE,
        'announcementTime': 1788278400000,
        'adjunctUrl': f'finalpage/2026-09-02/{pdf_id}.PDF',
    }
    for code, pdf_id in [('161130', '1225541045'), ('161125', '1225541050')]
}


def _post(code, label, url, *, post, json=None, data=None, headers=None):
    try:
        response = post(url, json=json, data=data, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise FreshnessError(f'{code}: {label} disclosure query failed: {exc}') from exc
    if not isinstance(payload, dict):
        raise FreshnessError(f'{code}: {label} disclosure response is not an object')
    return payload


def _count(value, code, label):
    if type(value) is not int or value < 0:
        raise FreshnessError(f'{code}: {label} disclosure count is invalid')
    return value


def _efunds_bonus(post, code):
    payload = _post(code, 'E Fund bonus', EFUNDS_BONUS, post=post, json={
        'fundCode': code, 'pageIndex': 0, 'pageSize': PAGE_SIZE,
        'startDate': '', 'endDate': '',
    }, headers=UA)
    if type(payload.get('status')) is not int or payload['status'] != 1 \
            or _count(payload.get('total'), code, 'E Fund bonus') != 0 \
            or payload.get('data') != []:
        raise FreshnessError(f'{code}: E Fund bonus history is nonempty or incomplete')


def _efunds_notices(post, code, first, last, *, expect_empty):
    payload = _post(code, 'E Fund notices', EFUNDS_NOTICES, post=post, json={
        'siteID': '1', 'catalogAlias': 'xxplflwj,xxpldqgg,xxpllsgg',
        'title': '', 'fundCode': code, 'isIncludeTransFund': 'Y',
        'prop1': f'{first:%Y-%m-%d},{last:%Y-%m-%d}',
        'pageSize': PAGE_SIZE, 'pageIndex': 0,
    }, headers=UA)
    body = payload.get('data')
    if (type(payload.get('status')) is not int or payload['status'] != 1
            or not isinstance(body, dict)):
        raise FreshnessError(f'{code}: E Fund notices response is incomplete')
    total = _count(body.get('total'), code, 'E Fund notices')
    rows = body.get('data')
    if not isinstance(rows, list) or total > PAGE_SIZE or len(rows) != total:
        raise FreshnessError(f'{code}: E Fund notices pagination is incomplete')
    if expect_empty:
        if total != 0:
            raise FreshnessError(f'{code}: E Fund notice found')
    else:
        expected = {(813013, _SUSPENSION_TITLE), EFUNDS_CONTROL_REPORTS[code]}
        if (total != 2 or any(not isinstance(row, dict) for row in rows)
                or any(type(row.get('id')) is not int or not isinstance(row.get('title'), str)
                       for row in rows)
                or {(row.get('id'), row.get('title')) for row in rows} != expected):
            raise FreshnessError(f'{code}: E Fund notices positive control changed')


def _cninfo(post, code, first, last, *, expect_empty):
    payload = _post(code, 'CNINFO notices', CNINFO_NOTICES, post=post, data={
        'stock': f'{code},jjjl0000041', 'pageNum': 1, 'pageSize': 30,
        'column': 'fund', 'tabName': 'fulltext', 'plate': '', 'searchkey': '',
        'secid': '', 'category': '', 'trade': '',
        'seDate': f'{first:%Y-%m-%d}~{last:%Y-%m-%d}',
        'sortName': '', 'sortType': '', 'isHLtitle': 'true',
    }, headers={**UA, 'Referer': 'https://www.cninfo.com.cn/'})
    total = _count(payload.get('totalAnnouncement'), code, 'CNINFO announcements')
    records = _count(payload.get('totalRecordNum'), code, 'CNINFO records')
    rows = payload.get('announcements')
    if payload.get('hasMore') is not False or total != records:
        raise FreshnessError(f'{code}: CNINFO notices pagination is incomplete')
    if expect_empty:
        if total == 0 and rows in (None, []):
            return []
        if (total == 1 and isinstance(rows, list) and len(rows) == 1
                and isinstance(rows[0], dict)
                and all(rows[0].get(key) == value
                        for key, value in AUDITED_CNINFO_NOTICES[code].items())):
            return [AUDITED_CNINFO_NOTICES[code]['adjunctUrl']]
        raise FreshnessError(f'{code}: CNINFO unaudited notice found')
    elif (total != 1 or not isinstance(rows, list) or len(rows) != 1
          or not isinstance(rows[0], dict)
          or any(rows[0].get(key) != value
                 for key, value in CNINFO_CONTROL_NOTICES[code].items())):
        raise FreshnessError(f'{code}: CNINFO positive control changed')
    return []


def verify_no_actions(code, through_date, *, now=None, post=None):
    """Return audited provenance, or fail closed before raw-return conversion.

    ``through_date`` is the newest required China market close. Notices are
    checked through the *current Beijing date*, including pre-open releases.
    """
    if code not in AUDITED_CNINFO_NOTICES:
        raise FreshnessError(f'{code}: no official-action bridge is configured')
    try:
        market_date = date.fromisoformat(str(through_date))
    except ValueError as exc:
        raise FreshnessError(f'{code}: invalid market date for disclosure check') from exc
    if market_date < ANCHOR:
        raise FreshnessError(f'{code}: disclosure bridge precedes frozen anchor')
    checked_at = datetime.now(timezone.utc) if now is None else now
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise FreshnessError(f'{code}: disclosure check needs a timezone-aware clock')
    checked_at = checked_at.astimezone(timezone.utc)
    beijing_date = checked_at.astimezone(ZoneInfo('Asia/Shanghai')).date()
    if market_date > beijing_date:
        raise FreshnessError(f'{code}: market date is in the future')
    notice_through = max(market_date, beijing_date)
    post = requests.post if post is None else post

    _efunds_bonus(post, code)
    _efunds_notices(post, code, date(2026, 8, 31), NOTICE_START, expect_empty=False)
    _efunds_notices(post, code, NOTICE_START, notice_through, expect_empty=True)
    _cninfo(post, code, date(2026, 9, 2), date(2026, 9, 2), expect_empty=False)
    audited = _cninfo(post, code, NOTICE_START, notice_through, expect_empty=True)
    return {
        'fund_code': code,
        'market_through': market_date.isoformat(),
        'notices_from': NOTICE_START.isoformat(),
        'notices_through': notice_through.isoformat(),
        'checked_at': checked_at.isoformat(),
        'sources': ['efunds_bonus_all_history', 'efunds_all_notices', 'cninfo_fund_notices'],
        'audited_non_action_notices': audited,
    }

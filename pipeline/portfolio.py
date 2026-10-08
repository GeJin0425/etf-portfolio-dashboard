"""实盘组合模拟: 起始日建仓 + 每季度最后交易日收盘再平衡。

费率: max(成交额 * 佣金率, 单笔最低佣金)。
"""

import pandas as pd


def _fee(notional, comm, min_comm):
    return max(notional * comm, min_comm)


def _quarter_ends(start_year, end_year):
    ends = []
    for year in range(start_year, end_year + 1):
        for month in (3, 6, 9, 12):
            ends.append(pd.Timestamp(f'{year}-{month:02d}-28') + pd.offsets.MonthEnd(0))
    return ends


def run_portfolio(closes, weights, start='2026-01-05', initial=100000,
                  comm=0.00005, min_comm=0.5, strategy=None, bond_opens=None,
                  snapshot=None):
    """closes: {code: Series(close)}; weights: [(code, target_weight)]"""
    if abs(sum(w for _, w in weights) - 1.0) > 1e-9:
        raise ValueError('权重之和必须为1')

    idx = sorted(set().union(*[set(closes[code].index) for code, _ in weights]))
    idx = [d for d in idx if d >= pd.Timestamp('2026-09-30' if snapshot else start)]
    if not idx:
        raise ValueError('起始日之后没有任何行情数据')
    common = pd.DatetimeIndex(idx)
    aligned = {code: closes[code].reindex(common).ffill() for code in closes}
    if any(pd.isna(aligned[code].iloc[0]) for code, _ in weights):
        raise ValueError('起始日缺少某只标的的价格')
    switch = pd.Timestamp('2026-09-30')
    if strategy and common[-1] >= switch:
        if switch not in common:
            raise ValueError('缺少 2026-09-30 季末调仓行情')
        for code in ('510880', '511260'):
            if code not in aligned or aligned[code].loc[switch:].isna().any():
                raise ValueError(f'{code} 策略期间缺少收盘行情')
        if bond_opens is None or bond_opens.reindex(common[common > switch]).isna().any():
            raise ValueError('511260 策略期间缺少开盘行情')
        missing_fills = {pd.Timestamp(f['date']) for f in strategy['fills']} - set(common)
        if missing_fills:
            raise ValueError(f'510880 策略成交日缺少组合行情: {min(missing_fills):%Y-%m-%d}')

    rb_dates = set()
    for qe in _quarter_ends(pd.Timestamp(start).year, common[-1].year):
        if qe > common[-1]:
            continue  # 季度末尚未到来, 不能提前触发
        candidates = [d for d in common if d <= qe]
        if candidates and (candidates[-1] > common[0] or snapshot and candidates[-1] == common[0]):
            rb_dates.add(candidates[-1])

    def fee(v):
        return _fee(v, comm, min_comm)

    cash = float(snapshot['cash'] if snapshot else initial)
    # Opening commissions are already reflected in cash / first-day NAV.
    # The frozen snapshot's fees_paid only contains later rebalance fees.
    fees_paid = (float(snapshot['fees_paid']) + float(snapshot['opening_fees'])
                 if snapshot else 0.0)
    shares = {code: 0 for code in closes}
    if snapshot:
        shares.update(snapshot['shares'])
    else:
        for code, weight in weights:
            p0 = float(aligned[code].iloc[0])
            target = initial * weight
            s = int((target - fee(target)) / p0 / 100) * 100
            cost = s * p0 + fee(s * p0)
            if cost > cash:
                s = int((cash - fee(cash)) / p0 / 100) * 100
                cost = s * p0 + fee(s * p0)
            if s:
                cash -= cost
                fees_paid += fee(s * p0)
            shares[code] = s

    def value_at(d):
        return cash + sum(shares[code] * float(aligned[code].loc[d])
                          for code in shares if shares[code])

    equity = [(pd.Timestamp(d), float(v)) for d, v in snapshot['equity']] if snapshot else []
    rebalances = list(snapshot['rebalances']) if snapshot else []
    strategy_trades = []
    fills = {pd.Timestamp(f['date']): f for f in strategy['fills']} if strategy else {}
    sleeve = None
    sleeve_cash = 0.0

    def rotate(d, old, new, old_price, new_price, signal_date=None, reason=''):
        nonlocal cash, fees_paid, sleeve_cash
        budget = sleeve_cash
        if shares[old]:
            n = shares[old]
            amount = n * old_price
            f = fee(amount)
            cash += amount - f
            budget += amount - f
            fees_paid += f
            shares[old] = 0
            strategy_trades.append({'date': d.strftime('%Y-%m-%d'), 'signal_date': signal_date,
                                    'code': old, 'action': '卖出', 'shares': n,
                                    'price': round(old_price, 3), 'fee': round(f, 2), 'reason': reason})
        n = max(0, int((budget - fee(budget)) / new_price / 100) * 100)
        while n and n * new_price + fee(n * new_price) > budget + 1e-9:
            n -= 100
        if n:
            amount = n * new_price
            f = fee(amount)
            cash -= amount + f
            fees_paid += f
            shares[new] += n
            sleeve_cash = budget - amount - f
            strategy_trades.append({'date': d.strftime('%Y-%m-%d'), 'signal_date': signal_date,
                                    'code': new, 'action': '买入', 'shares': n,
                                    'price': round(new_price, 3), 'fee': round(f, 2), 'reason': reason})
        else:
            sleeve_cash = budget

    for d in common:
        if strategy and d > switch and d in fills:
            fill = fills[d]
            next_sleeve = '510880' if fill['action'] == 'BUY' else '511260'
            if sleeve == next_sleeve:
                raise ValueError(f'510880 重复策略成交: {d:%Y-%m-%d}')
            bond_open = float(bond_opens.loc[d])
            rotate(d, sleeve, next_sleeve,
                   bond_open if sleeve == '511260' else float(fill['price']),
                   bond_open if next_sleeve == '511260' else float(fill['price']),
                   fill['signal_date'], fill['reason'])
            sleeve = next_sleeve
        if strategy and d == switch:
            sleeve = strategy['switch_asset']
        if d in rb_dates:
            v_before = value_at(d)
            total_fee = 0.0
            shares_before = dict(shares)
            targets = {}
            target_weights = [(code, 0.0 if code == '159263' and sleeve else weight)
                              for code, weight in weights]
            if sleeve:
                target_weights.append((sleeve, next(w for c, w in weights if c == '159263')))
            for code, weight in target_weights:
                p = float(aligned[code].loc[d])
                t = v_before * weight
                targets[code] = max(0, int((t - fee(t)) / p / 100) * 100)

            trades = []
            for code, _ in target_weights:
                cur, tgt = shares[code], targets[code]
                if cur > tgt:
                    p = float(aligned[code].loc[d])
                    n = cur - tgt
                    amount = n * p
                    f = fee(amount)
                    total_fee += f
                    cash += amount - f
                    shares[code] = tgt
                    trades.append({
                        'code': code, 'action': '卖出', 'shares': n,
                        'price': round(p, 3), 'amount': round(amount, 2),
                        'fee': round(f, 2),
                    })

            buy_needs = []
            for code, _ in target_weights:
                cur, tgt = shares[code], targets[code]
                if cur < tgt:
                    p = float(aligned[code].loc[d])
                    buy_needs.append((code, cur, tgt, p, (tgt - cur) * p))
            # 现金不足以覆盖全部买入目标时, 优先满足偏离目标权重最多(金额最大)的资产,
            # 避免固定按 ASSETS 顺序导致排在后面的资产总是被牺牲
            buy_needs.sort(key=lambda item: -item[4])

            for code, cur, tgt, p, _ in buy_needs:
                need = tgt - cur
                amount = need * p
                f = fee(amount)
                if cash >= amount + f:
                    cash -= amount + f
                    total_fee += f
                    shares[code] = tgt
                    trades.append({
                        'code': code, 'action': '买入', 'shares': need,
                        'price': round(p, 3), 'amount': round(amount, 2),
                        'fee': round(f, 2),
                    })
                else:
                    s = int((cash - fee(cash)) / p / 100) * 100
                    if s > 0:
                        amount = s * p
                        f = fee(amount)
                        total_fee += f
                        cash -= amount + f
                        shares[code] = cur + s
                        trades.append({
                            'code': code, 'action': '买入(部分)', 'shares': s,
                            'price': round(p, 3), 'amount': round(amount, 2),
                            'fee': round(f, 2),
                        })

            v_after = value_at(d)
            if sleeve:
                sleeve_cash = min(cash, max(0.0, v_after * next(
                    w for c, w in weights if c == '159263') -
                    shares[sleeve] * float(aligned[sleeve].loc[d])))
            fees_paid += total_fee
            rebalances.append({
                'date': d.strftime('%Y-%m-%d'),
                'value_before': round(v_before, 2),
                'value_after': round(v_after, 2),
                'fee': round(total_fee, 2),
                'trades': trades,
                'weights_before': [
                    {'code': code, 'pct': round(shares_before[code] * float(aligned[code].loc[d]) / v_before * 100, 1)}
                    for code, _ in target_weights if shares_before[code] or targets[code]
                ],
                'weights_after': [
                    {'code': code, 'pct': round(shares[code] * float(aligned[code].loc[d]) / v_after * 100, 1)}
                    for code, _ in target_weights if shares[code] or targets[code]
                ],
            })

        equity.append((d, value_at(d)))

    eq = pd.Series([v for _, v in equity], index=[d for d, _ in equity])
    return {
        'equity': eq,
        'shares': shares,
        'cash': cash,
        'rebalances': rebalances,
        'fees_paid': fees_paid,
        'aligned': aligned,
        'strategy_trades': strategy_trades,
        'sleeve_asset': sleeve,
    }

"""Local USD betting ledger. Integer cents separate cash funding from realized P&L."""
from contextlib import contextmanager
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
import csv
from hashlib import sha256
from io import StringIO
import json
import re
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
ZONE=ZoneInfo('America/Indianapolis')
STATUSES=('open','won','lost','push','void','cashed_out','settled')
FIELDS=['record_type','external_id','account','currency','placed_at','sport','description','market','stake','american_odds',
        'status','settled_at','gross_return','fees','placement_fees','amount','notes']


def money(value,*,signed=False):
    try:
        if isinstance(value,bool):raise ValueError('Enter a monetary amount.')
        amount=Decimal(str(value))
        if not amount.is_finite() or abs(amount)>Decimal('1000000000'):raise ValueError('Enter a finite amount no larger than $1 billion.')
        if amount!=amount.quantize(Decimal('.01')):raise ValueError('Money amounts may have at most two decimal places.')
        if not signed and amount<0:raise ValueError('Enter a nonnegative amount.')
        return int(amount*100)
    except (InvalidOperation,TypeError):raise ValueError('Enter a valid monetary amount.') from None


def stamp(value=None,*,required=False):
    if not value:
        if required:raise ValueError('Imported records require explicit dates.')
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')
    try:
        parsed=datetime.fromisoformat(str(value).strip().replace('Z','+00:00'))
        if parsed.tzinfo is None:
            if parsed.replace(tzinfo=ZONE,fold=0).utcoffset()!=parsed.replace(tzinfo=ZONE,fold=1).utcoffset():
                raise ValueError('Use an explicit UTC offset during a daylight-saving clock change.')
            parsed=parsed.replace(tzinfo=ZONE)
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError,TypeError) as exc:raise ValueError('Enter an ISO date/time, with a UTC offset for daylight-saving clock changes.') from exc


def text(value,label,required=False,maximum=500):
    result=str(value or '').strip()
    if (required and not result) or len(result)>maximum:raise ValueError(f'{label} is required and must be at most {maximum} characters.' if required else f'{label} must be at most {maximum} characters.')
    return result


def odds(value):
    if value in {None,''}:return None
    try:
        number=Decimal(str(value))
        if not number.is_finite() or not 100<=abs(number)<=1000000:raise ValueError('American odds must be at least +100 or at most −100.')
        return format(number.normalize(),'f')
    except InvalidOperation:raise ValueError('Enter valid American odds or leave them blank.') from None


def settlement(stake,american,status,gross=None,fees=0):
    if status not in STATUSES:raise ValueError('Choose a supported bet status.')
    american=odds(american)
    fee=money(fees or 0)
    if status=='open':
        if gross not in {None,''} or fee:raise ValueError('Open bets cannot have a return or settlement fees. Map paid trade/fill fees to placement fees.')
        return None,0
    if gross not in {None,''}:returned=money(gross)
    elif status in {'push','void'}:returned=stake
    elif status=='lost':returned=0
    elif status=='won' and american is not None:
        a=Decimal(american);profit=Decimal(stake)*(a/100 if a>0 else 100/-a)
        returned=stake+int(profit.quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    else:raise ValueError('Enter the actual gross return, including returned stake.')
    if status=='lost' and returned!=0:raise ValueError('Lost bets must have zero gross return.')
    if status in {'push','void'} and returned!=stake:raise ValueError('Push/void returns must equal the stake; record charges as fees.')
    if status=='won' and returned<=stake:raise ValueError('A winning return must exceed the stake. Use Settled or Cashed out for other returns.')
    return returned,fee


def record(values,*,importing=False):
    kind=text(values.get('record_type'),'Record type',True)
    if kind not in {'bet','opening','deposit','withdrawal','adjustment'}:raise ValueError('Record type must be bet, opening, deposit, withdrawal or adjustment.')
    account=text(values.get('account'),'Account',True,100)
    if ' '.join(account.casefold().split())=='__all__':raise ValueError('Choose a named account.')
    if str(values.get('currency') or 'USD').strip().upper()!='USD':raise ValueError('This ledger records USD only; convert other currencies before importing.')
    result=dict(record_type=kind,account=account,account_key=' '.join(account.casefold().split()),currency='USD',
        external_id=text(values.get('external_id'),'External ID',importing,200) or None,
        placed_at=stamp(values.get('placed_at'),required=importing),notes=text(values.get('notes'),'Notes'))
    if kind!='bet':
        amount=money(values.get('amount'),signed=kind=='adjustment')
        if kind not in {'opening'} and amount==0:raise ValueError('Enter a nonzero cash transaction.')
        result['amount']=(-amount if kind=='withdrawal' else amount)
        return result
    stake=money(values.get('stake'))
    if stake<=0:raise ValueError('Stake must be greater than zero.')
    status=values.get('status') or 'open';american=odds(values.get('american_odds'))
    returned,fee=settlement(stake,american,status,values.get('gross_return'),values.get('fees',0))
    closed=stamp(values.get('settled_at'),required=importing) if status!='open' else None
    if closed and closed<result['placed_at']:raise ValueError('Settlement cannot precede placement.')
    result.update(stake=stake,american_odds=american,status=status,settled_at=closed,gross_return=returned,fees=fee,
                  placement_fees=money(values.get('placement_fees') or 0),
                  sport=text(values.get('sport') or 'Other','Sport',True,30),description=text(values.get('description'),'Bet description',True),
                  market=text(values.get('market'),'Market',maximum=200))
    return result


class Ledger:
    def __init__(self,path=None):self.path=Path(path or ROOT/'data/pnl/history.sqlite3')

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=15)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, kind TEXT, account_key TEXT, external_id TEXT, payload TEXT, UNIQUE(account_key,external_id))')
                db.execute('CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, result TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, record_id TEXT, changed_at TEXT, before_json TEXT, after_json TEXT)')
                yield db
        finally:db.close()

    def write(self,operation,values,request_id):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('Reload the page before saving.')
        request_hash=sha256(json.dumps([operation,values],sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT result FROM requests WHERE id=?',(request_id,)).fetchone()
            if prior:
                saved=json.loads(prior[0])
                if saved.pop('_request_hash',None)!=request_hash:raise ValueError('This save request already recorded different data. Refresh the page before trying again.')
                return saved
            if operation=='settle':
                old=db.execute('SELECT payload FROM records WHERE id=? AND kind=?',(values.get('id'),'bet')).fetchone()
                if not old:raise ValueError('Choose a recorded bet.')
                before=json.loads(old[0]);after=dict(before)
                status=values.get('status');returned,fee=settlement(before['stake'],before['american_odds'],status,values.get('gross_return'),values.get('fees',0))
                at=stamp(values.get('settled_at')) if status!='open' else None
                if at and at<before['placed_at']:raise ValueError('Settlement cannot precede placement.')
                after.update(status=status,settled_at=at,gross_return=returned,fees=fee)
                db.execute('UPDATE records SET payload=? WHERE id=?',(json.dumps(after),after['id']))
                db.execute('INSERT INTO revisions(record_id,changed_at,before_json,after_json) VALUES(?,?,?,?)',(after['id'],stamp(),json.dumps(before),json.dumps(after)))
                result=dict(added=0,skipped=0,message='Settlement saved.')
            else:
                raw=values if operation=='import' else [values]
                if operation not in {'import','add'} or not isinstance(raw,list) or not raw or len(raw)>10000:raise ValueError('Import 1–10,000 rows at a time.')
                records=[record(item,importing=operation=='import') for item in raw]
                added=skipped=0
                for item in records:
                    existing=db.execute('SELECT payload FROM records WHERE account_key=? AND (external_id=? OR id=?)',
                                        (item['account_key'],item['external_id'],item['external_id'])).fetchone() if item['external_id'] else None
                    if existing:
                        original=json.loads(existing[0])
                        if original['record_type']=='bet':original.setdefault('placement_fees',0)
                        if original['external_id'] is None and item['external_id']==original['id']:item['external_id']=None
                        original.pop('id',None)
                        if original!=item:raise ValueError('An external ID already exists with different data. Correct its settlement through the page; imports do not overwrite history.')
                        skipped+=1;continue
                    if item['record_type']=='opening' and db.execute('SELECT 1 FROM records WHERE account_key=? AND kind=?',(item['account_key'],'opening')).fetchone():
                        raise ValueError('This account already has an opening balance. Use a deposit, withdrawal or adjustment.')
                    item['id']=uuid4().hex
                    db.execute('INSERT INTO records VALUES(?,?,?,?,?)',(item['id'],item['record_type'],item['account_key'],item['external_id'],json.dumps(item)))
                    db.execute('INSERT INTO revisions(record_id,changed_at,before_json,after_json) VALUES(?,?,?,?)',(item['id'],stamp(),None,json.dumps(item)))
                    added+=1
                result=dict(added=added,skipped=skipped,message=f'{added} records saved; {skipped} duplicate records skipped.')
            db.execute('INSERT INTO requests VALUES(?,?)',(request_id,json.dumps({**result,'_request_hash':request_hash})))
            return result

    def records(self):
        with self.connect() as db:return [json.loads(row[0]) for row in db.execute('SELECT payload FROM records ORDER BY rowid')]


def history(records,account='__all__'):
    chosen=[r for r in records if account=='__all__' or r['account_key']==account]
    bets=[r for r in chosen if r['record_type']=='bet'];closed=[r for r in bets if r['status']!='open']
    events=[]
    for r in chosen:
        if r['record_type']!='bet':
            events.append(dict(at=r['placed_at'],order=0,account=r['account'],type=r['record_type'],description=r.get('notes',''),cash=r['amount'],open=0,pnl=0))
        else:
            entry_fee=r.get('placement_fees',0)
            events.append(dict(at=r['placed_at'],order=1,account=r['account'],type='Stake',description=r['description'],cash=-r['stake']-entry_fee,open=r['stake'],pnl=-entry_fee))
            if r['status']!='open':
                events.append(dict(at=r['settled_at'],order=2,account=r['account'],type=r['status'],description=r['description'],
                    cash=r['gross_return']-r['fees'],open=-r['stake'],pnl=r['gross_return']-r['fees']-r['stake']))
    cash=exposure=profit=0;timeline=[]
    for event in sorted(events,key=lambda e:(e['at'],e['order'])):
        cash+=event['cash'];exposure+=event['open'];profit+=event['pnl']
        timeline.append({**event,'cash_balance':cash,'open_stake':exposure,'bankroll':cash+exposure,'cumulative_pnl':profit})
    risk=sum(r['stake'] for r in closed);wins=sum(r['status']=='won' for r in closed);losses=sum(r['status']=='lost' for r in closed)
    closed_profit=sum(r['gross_return']-r['fees']-r['stake']-r.get('placement_fees',0) for r in closed)
    return dict(records=chosen,bets=bets,timeline=timeline,cash=cash,open_stake=exposure,bankroll=cash+exposure,pnl=profit,
                roi=closed_profit/risk if risk else None,win_rate=wins/(wins+losses) if wins+losses else None,settled=len(closed))


def parse_csv(value):
    reader=csv.DictReader(StringIO(value.lstrip('\ufeff')))
    if not reader.fieldnames or not {'record_type','account','external_id','placed_at'}<=set(reader.fieldnames):raise ValueError('Use the ledger CSV template and its column names.')
    result=[]
    for index,row in enumerate(reader):
        if index>=10000:raise ValueError('Import no more than 10,000 rows.')
        if None in row:raise ValueError('A CSV row has extra columns; check quoted text.')
        if any(v for v in row.values()):result.append({k:v for k,v in row.items() if v is not None and v!=''})
    return result


ALIASES={
    'external_id':['externalid','betid','wagerid','ticketid','transactionid','referenceid','id'],
    'account':['account','accountname','sportsbook','bookmaker','platform'],
    'record_type':['recordtype','transactiontype','activitytype'],
    'placed_at':['placedat','dateplaced','placeddate','betdate','timestamp','date','datetime','createdat'],
    'settled_at':['settledat','datesettled','settlementdate','settleddate','resolvedat'],
    'description':['description','selection','betdescription','bet','event','title'],
    'stake':['stake','betamount','wageramount','wager','risk','cost','totalcost'],
    'gross_return':['grossreturn','payout','totalpayout','return','winnings','netprofit','profit','netpnl','pnl'],
    'american_odds':['americanodds','odds'], 'fees':['fees','fee','commission','totalfees'], 'placement_fees':['placementfees','entryfees','tradefees','fillfees'],
    'status':['status','outcome','result','betstatus'], 'amount':['amount','transactionamount'],
    'sport':['sport','league'], 'market':['market','bettype'], 'currency':['currency'], 'notes':['notes','note']}


def statement_rows(value):
    try:
        reader=csv.DictReader(StringIO(value.lstrip('\ufeff')))
        columns=reader.fieldnames
        if not columns or len(columns)!=len(set(columns)):raise ValueError('The CSV needs unique column headings.')
        rows=[]
        for index,row in enumerate(reader):
            if index>=10000:raise ValueError('Import no more than 10,000 rows.')
            if None in row:raise ValueError('A CSV row has extra columns. Check quoted descriptions.')
            if any(row.values()):rows.append(row)
        if not rows:raise ValueError('The statement has no records.')
        return columns,rows
    except csv.Error:raise ValueError('The CSV could not be parsed. Check its formatting.') from None


def column_mapping(columns):
    indexed={re.sub(r'[^a-z0-9]','',name.casefold()):name for name in columns}
    return {field:next((indexed[name] for name in ALIASES.get(field,[]) if name in indexed),None) for field in FIELDS}


def mapped_statement(rows,mapping,account,kind='bet',status='open',basis='gross',date_format='iso',debits=False,fees_at='settlement'):
    if basis not in {'gross','profit','net'}:raise ValueError('Choose whether the return column contains gross return, profit before fees or profit after fees.')
    formats={'us-time':'%m/%d/%Y %H:%M:%S','us-minute':'%m/%d/%Y %H:%M','us-ampm':'%m/%d/%Y %I:%M %p','us-date':'%m/%d/%Y'}
    if date_format not in {'iso',*formats}:raise ValueError('Choose a date format.')
    if fees_at not in {'placement','settlement'}:raise ValueError('Choose when the mapped fee column is charged.')
    def amount(value):
        raw=str(value).strip()
        if not re.fullmatch(r'\(?[+-]?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\)?',raw):raise ValueError('Use monetary values with at most two decimal places.')
        if raw.startswith('('):
            if not raw.endswith(')') or '+' in raw or '-' in raw:raise ValueError('Invalid negative amount.')
            raw='-'+raw[1:-1]
        elif ')' in raw:raise ValueError('Invalid amount.')
        return Decimal(raw.replace('$','').replace(',',''))
    result=[]
    for index,source in enumerate(rows,2):
        try:
            values={key:source.get(column) for key,column in mapping.items() if column and source.get(column) not in {None,''}}
            values.setdefault('account',account);values.setdefault('record_type',kind);values.setdefault('status',status)
            values['record_type']={'wager':'bet','opening balance':'opening','starting balance':'opening'}.get(str(values['record_type']).casefold().strip(),str(values['record_type']).casefold().strip())
            outcome=re.sub(r'[ _-]+',' ',str(values['status']).casefold().strip())
            values['status']={'pending':'open','unsettled':'open','win':'won','loss':'lost','voided':'void','cancelled':'void','canceled':'void',
                              'cashout':'cashed_out','cash out':'cashed_out','cashedout':'cashed_out','cashed out':'cashed_out','closed':'settled'}.get(outcome,outcome)
            for field in ['placed_at','settled_at']:
                if values.get(field) and date_format!='iso':values[field]=datetime.strptime(values[field].strip(),formats[date_format]).isoformat()
            for field in ['stake','gross_return','fees','placement_fees','amount']:
                if values.get(field) not in {None,''}:
                    number=amount(values[field])
                    if debits and field in {'stake','fees','placement_fees'}:number=abs(number)
                    if field=='amount' and values['record_type']=='withdrawal':number=abs(number)
                    values[field]=str(number)
            if fees_at=='placement' and values.get('fees') is not None:
                values['placement_fees']=str(Decimal(values.get('placement_fees',0))+Decimal(values['fees']));values['fees']='0'
            if values['status']=='open' and Decimal(values.get('gross_return',0))==0:values.pop('gross_return',None)
            if values.get('gross_return') is not None and basis!='gross':
                values['gross_return']=str(Decimal(values['gross_return'])+Decimal(values.get('stake',0))+
                    (Decimal(values.get('fees',0))+Decimal(values.get('placement_fees',0)) if basis=='net' else 0))
            record(values,importing=True)
            result.append(values)
        except (ValueError,InvalidOperation,TypeError) as exc:raise ValueError(f'CSV row {index}: {exc}') from None
    if not result:raise ValueError('The statement has no records.')
    return result


def csv_text(records=None):
    output=StringIO();writer=csv.DictWriter(output,fieldnames=FIELDS);writer.writeheader()
    for item in records or []:
        row={k:item.get(k,'') for k in FIELDS}
        # Stable IDs allow an exported ledger to be reimported without duplication.
        row['external_id']=item.get('external_id') or item['id']
        for field in ['stake','gross_return','fees','placement_fees','amount']:
            if item.get(field) is not None:row[field]=f"{Decimal(abs(item[field]) if field=='amount' and item['record_type']=='withdrawal' else item[field])/100:.2f}"
        writer.writerow(row)
    return output.getvalue()

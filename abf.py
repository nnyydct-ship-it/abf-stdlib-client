#!/usr/bin/env python3
"""Dependency-free reader for Agent Bounty Feed's free MCP endpoint."""
import argparse
import datetime
import json
import math
import sys
import time
import urllib.error
import urllib.request

ENDPOINT='https://agent-bounty-feed.dailydollar.workers.dev/mcp'
VERSIONS=('2025-06-18','2025-03-26','2024-11-05')
MAX_BYTES=2*1024*1024

class ClientError(Exception):
    def __init__(self,code,message,retry_after=None):
        super().__init__(message);self.code=code;self.retry_after=retry_after

def matching_reply(value,request_id):
    if isinstance(request_id,bool) or not isinstance(request_id,(str,int,float)) or request_id is None:
        raise ClientError('rpc_mismatch','Invalid JSON-RPC request ID')
    rows=value if isinstance(value,list) else [value]
    found=[x for x in rows if isinstance(x,dict) and not isinstance(x.get('id'),bool) and x.get('id')==request_id]
    if len(found)!=1 or found[0].get('jsonrpc')!='2.0':
        raise ClientError('rpc_mismatch','Expected one matching JSON-RPC response')
    return found[0]

def read_reply(response,request_id):
    media=response.headers.get('Content-Type','').split(';')[0].strip().lower()
    try:
        if media=='application/json':
            raw=response.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise ClientError('response_too_large','Response exceeds 2 MiB')
            return matching_reply(json.loads(raw.decode('utf-8')),request_id)
        if media=='text/event-stream':
            total=0;parts=[];started=time.monotonic()
            while True:
                raw=response.readline(MAX_BYTES+1)
                if not raw:break
                total+=len(raw)
                if total>MAX_BYTES or time.monotonic()-started>30:
                    raise ClientError('stream_limit','SSE response exceeded its size/time limit')
                line=raw.decode('utf-8').rstrip('\r\n')
                if not line:
                    if parts:
                        value=json.loads('\n'.join(parts));parts=[]
                        candidates=value if isinstance(value,list) else [value]
                        if any(isinstance(v,dict) and v.get('id')==request_id for v in candidates):
                            return matching_reply(value,request_id)
                elif line.startswith('data:'):
                    parts.append(line[5:].lstrip(' '))
            if parts:return matching_reply(json.loads('\n'.join(parts)),request_id)
            raise ClientError('missing_reply','SSE ended without a matching reply')
        raise ClientError('unexpected_content_type','Expected JSON or an MCP SSE response')
    except (ValueError,UnicodeError) as exc:
        raise ClientError('invalid_response','Server response is not valid UTF-8 JSON') from exc

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ClientError('redirect_refused','The endpoint redirected; inspect it before continuing')

class HTTPTransport:
    def __init__(self):
        self.opener=urllib.request.build_opener(NoRedirect)
        self.session_id=None
    def post(self,message,version=None):
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream',
                 'User-Agent':'ABF-stdlib-client/1.0 (Python; free MCP discovery)'}
        if version:headers['MCP-Protocol-Version']=version
        if self.session_id:headers['Mcp-Session-Id']=self.session_id
        request=urllib.request.Request(ENDPOINT,json.dumps(message).encode(),headers,method='POST')
        try:
            with self.opener.open(request,timeout=20) as response:
                if response.headers.get('Mcp-Session-Id'):
                    self.session_id=response.headers['Mcp-Session-Id']
                if 'id' not in message:
                    if response.status not in (200,202,204):
                        raise ClientError('notification_refused','Initialization notification was refused')
                    return None
                return read_reply(response,message['id'])
        except urllib.error.HTTPError as exc:
            description={402:'Payment requested; this client has no payment flow',429:'Rate limited; respect Retry-After'}
            retry_after=exc.headers.get('Retry-After');code=exc.code
            exc.close()
            raise ClientError('http_'+str(code),description.get(code,'HTTP request refused'),retry_after) from exc
        except (urllib.error.URLError,TimeoutError,OSError) as exc:
            raise ClientError('network_error','Endpoint unavailable or timed out; no automatic retry') from exc

class MCPClient:
    def __init__(self,transport=None):
        self.transport=transport if transport is not None else HTTPTransport()
        self.version=None;self.next_id=1
    def rpc(self,method,params=None):
        request_id=self.next_id;self.next_id+=1
        payload={'jsonrpc':'2.0','id':request_id,'method':method}
        if params is not None:payload['params']=params
        reply=self.transport.post(payload,self.version)
        reply=matching_reply(reply,request_id)
        if 'error' in reply:
            error=reply['error'];raise ClientError('rpc_'+str(error.get('code')),str(error.get('message','RPC failed'))[:300])
        if not isinstance(reply.get('result'),dict):raise ClientError('invalid_result','MCP result must be an object')
        return reply['result']
    def initialize(self):
        if self.version:return
        result=self.rpc('initialize',{'protocolVersion':VERSIONS[0],'capabilities':{},
                                    'clientInfo':{'name':'abf-stdlib-client','version':'1.0.0'}})
        if result.get('protocolVersion') not in VERSIONS:
            raise ClientError('unsupported_protocol','Server selected an unsupported MCP protocol version')
        self.version=result['protocolVersion']
        self.transport.post({'jsonrpc':'2.0','method':'notifications/initialized'},self.version)
    def tools(self):
        self.initialize();return self.rpc('tools/list')
    def call_tool(self,name,arguments=None):
        if name not in ['list_bounties','get_new_bounties','get_bounty','get_feed_stats']:
            raise ClientError('unsupported_tool','Only the four free discovery tools are supported')
        self.initialize();result=self.rpc('tools/call',{'name':name,'arguments':arguments or {}})
        if result.get('isError'):
            texts=[x.get('text','') for x in result.get('content',[]) if x.get('type')=='text']
            raise ClientError('tool_error',' '.join(texts)[:300] or 'MCP tool returned an error')
        if isinstance(result.get('structuredContent'),dict):return result['structuredContent']
        for block in result.get('content',[]):
            if block.get('type')=='text':
                try:value=json.loads(block['text'])
                except ValueError:continue
                if isinstance(value,dict):return value
        raise ClientError('missing_data','Tool returned no JSON discovery object')

def compact(data):
    fields=['id','source','kind','title','reward','currency','est_usd','network','reward_basis',
            'pool_amount','per_worker_usd','requires','slots_left','deadline','url','first_seen','stale']
    def item(row):return {k:row[k] for k in fields if k in row}
    out={k:data[k] for k in ['generated_at','count','total_matching','has_more','next_since','next_after_id','sort'] if k in data}
    if 'items' in data:out['items']=[item(row) for row in data['items']]
    if 'item' in data:out['item']=item(data['item'])
    if 'stats' in data:
        stats=data['stats'];out['stats']={k:stats[k] for k in ['items','total_usd','by_source','new_last_24h','no_bond_no_stake_no_kyc','last_refresh','refresh_interval'] if k in stats}
    return out

def limit(value):
    n=int(value)
    if not 1<=n<=100:raise argparse.ArgumentTypeError('limit must be between 1 and 100')
    return n

def minimum(value):
    n=float(value)
    if not math.isfinite(n) or n<0:raise argparse.ArgumentTypeError('min-usd must be a finite nonnegative number')
    return n

def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',action='store_true',help='print the full free tool data instead of compact metadata')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('stats');sub.add_parser('tools')
    get=sub.add_parser('get');get.add_argument('id')
    for name in ['list','new']:
        p=sub.add_parser(name)
        p.add_argument('--limit',type=limit,default=5)
        p.add_argument('--min-usd',type=minimum,default=1)
        p.add_argument('--network',default='base')
        p.add_argument('--source');p.add_argument('--query')
        p.add_argument('--exclude',default='kyc,stake,bond,purchase,human_only')
        if name=='list':p.add_argument('--sort',choices=['reward','first_seen'],default='reward')
        else:
            p.add_argument('--since',required=True);p.add_argument('--after-id')
    return parser

def run(args,client):
    if args.command=='tools':return client.tools()
    if args.command=='stats':return client.call_tool('get_feed_stats')
    if args.command=='get':return client.call_tool('get_bounty',{'id':args.id})
    arguments={'limit':args.limit,'min_usd':args.min_usd,'network':args.network,'exclude_requires':args.exclude}
    if args.source:arguments['source']=args.source
    if args.query:arguments['q']=args.query
    if args.command=='list':arguments['sort']=args.sort
    else:
        try:date=datetime.datetime.fromisoformat(args.since.replace('Z','+00:00'))
        except ValueError as exc:raise ClientError('invalid_since','Use an ISO timestamp with timezone') from exc
        if date.tzinfo is None:raise ClientError('invalid_since','Use an ISO timestamp with timezone')
        arguments['since']=args.since
        if args.after_id:arguments['after_id']=args.after_id
    return client.call_tool('list_bounties' if args.command=='list' else 'get_new_bounties',arguments)

def main():
    args=build_parser().parse_args()
    try:
        result=run(args,MCPClient())
        print(json.dumps(result if args.raw or args.command=='tools' else compact(result),ensure_ascii=False,indent=2,allow_nan=False))
        return 0
    except ClientError as exc:
        error={'error':exc.code,'message':str(exc)}
        if exc.retry_after is not None:error['retry_after']=exc.retry_after
        print(json.dumps(error,ensure_ascii=False),file=sys.stderr);return 1

if __name__=='__main__':sys.exit(main())

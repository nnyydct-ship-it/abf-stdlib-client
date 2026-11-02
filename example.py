#!/usr/bin/env python3
"""List one current free discovery record, then fetch its exact ID."""
import json
import sys
from abf import MCPClient,ClientError,compact

def main():
    client=MCPClient()
    page=client.call_tool('list_bounties',{'network':'base','min_usd':1,'limit':1,
                                      'exclude_requires':'kyc,stake,bond,purchase,human_only'})
    result={'listing':compact(page)}
    if page.get('items'):
        try:result['detail']=compact(client.call_tool('get_bounty',{'id':page['items'][0]['id']}))
        except ClientError as exc:
            if exc.code!='tool_error':raise
            result['detail_changed_since_listing']=str(exc)
    else:result['detail']='No matching open item in this refresh'
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))

if __name__=='__main__':
    try:main()
    except ClientError as exc:
        print(json.dumps({'error':exc.code,'message':str(exc),'retry_after':exc.retry_after}),file=sys.stderr)
        sys.exit(1)

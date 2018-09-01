#!/usr/bin/env python
# -*- coding: UTF-8 -*-

from urllib import request
import json
from sql.extend_json_encoder import ExtendJSONEncoder



class WechatSender(object):


    # 获取access_token
    def GetToken(self, CorpID,Secret):
        tokenurl = 'https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=%s&corpsecret=%s' %(CorpID,Secret)
        response = request.urlopen(tokenurl)
        html = response.read().strip()
        ret = json.loads(html)
        access_token = ret['access_token']
        return access_token

    # 发送消息给指定用户
    def SendMessage(self, Token, User, Title, Message, URL):
        url = 'https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=%s' % Token
        UserID = '|'.join(User)
        values = {
           "touser" : UserID,
           "toparty" : "",
           "totag" : "",
           "msgtype" : "textcard",
           "agentid" : xxx,
           "textcard" : {
                    "title" : Title,
                    "description" : Message,
                    "url" : URL,
                    "btntxt":"详情"
           }
        }

        data = json.dumps(values, ensure_ascii=False, cls=ExtendJSONEncoder)
        req = request.Request(url, data.encode('utf8'))
        req.add_header('Content-Type', 'application/json')
        req.add_header('encoding', 'utf-8')
        response = request.urlopen(req)
        result = response.read().strip()
        result = json.loads(result)
        if result['errmsg'] != "ok":
            raise Exception('Something is wrong')

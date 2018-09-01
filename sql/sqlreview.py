# -*- coding: UTF-8 -*-
import simplejson as json

import time
from threading import Thread

from django.db import connection
from django.utils import timezone
from django.conf import settings

from .dao import Dao
from .const import Const, WorkflowDict
from .sendmail import MailSender
from .sendwechat import WechatSender
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, workflow, master_config
from .workflow import Workflow
from .permission import role_required, superuser_required
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
mailSender = MailSender()
prpCryptor = Prpcrypt()
workflowOb = Workflow()
wechatSender=WechatSender()

# 获取当前请求url
def getDetailUrl(request):
    scheme = request.scheme
    host = request.META['HTTP_HOST']
    return "%s://%s/detail/" % (scheme, host)


# 根据实例名获取主库连接字符串，并封装成一个dict
def getMasterConnStr(clusterName):
    listMasters = master_config.objects.filter(cluster_name=clusterName)

    masterHost = listMasters[0].master_host
    masterPort = listMasters[0].master_port
    masterUser = listMasters[0].master_user
    masterPassword = prpCryptor.decrypt(listMasters[0].master_password)
    dictConn = {'masterHost': masterHost, 'masterPort': masterPort, 'masterUser': masterUser,
                'masterPassword': masterPassword}
    return dictConn


# SQL工单执行回调
def execute_call_back(workflowId, clusterName, url):
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
    except ValueError:
        listAllReviewMen = (workflowDetail.review_man,)

    dictConn = getMasterConnStr(clusterName)
    try:
        # 交给inception先split，再执行
        logger.debug('execute_call_back:' + str(workflowId) + ' executing')
        (finalStatus, finalList) = inceptionDao.executeFinal(workflowDetail, dictConn)

        # 封装成JSON格式存进数据库字段里
        strJsonResult = json.dumps(finalList)
        workflowDetail = workflow.objects.get(id=workflowId)
        workflowDetail.execute_result = strJsonResult
        workflowDetail.finish_time = timezone.now()
        workflowDetail.status = finalStatus
        workflowDetail.is_manual = 0
        workflowDetail.audit_remark = ''
        # 重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
        logger.debug('execute_call_back:' + str(workflowId) + ' finish')
    except Exception as e:
        logger.error(e)

    # 如果执行完毕了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒.DBA需要知晓审核并执行过的单子
    if hasattr(settings, 'MAIL_ON_OFF') == True:
        if getattr(settings, 'MAIL_ON_OFF') == "on":
            # 给主、副审核人，申请人，DBA各发一封邮件
            engineer = workflowDetail.engineer
            reviewMen = workflowDetail.review_man
            workflowStatus = workflowDetail.status
            workflowName = workflowDetail.workflow_name
            objEngineer = users.objects.get(username=engineer)
            strTitle = "SQL上线工单执行完毕 # " + str(workflowId)
            strContent = "发起人：" + engineer + "\n审核人：" + reviewMen + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
            reviewManAddr = [email['email'] for email in
                             users.objects.filter(username__in=listAllReviewMen).values('email')]

            # 获取抄送人邮件地址
            workflowDetaiL = workflow.objects.get(id=workflowId)
            cc_list = json.loads(workflowDetaiL.email_cc)
            for cc_name in cc_list:
                cc_name_list = [email['email'] for email in
                                users.objects.filter(display=str(cc_name)).values('email')]
                for cc_addr in cc_name_list:
                    reviewManAddr.append(cc_addr)

            dbaAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
            listCcAddr = reviewManAddr + dbaAddr
            mailSender.sendEmail(strTitle, strContent, [objEngineer.email], listCcAddr=listCcAddr)

            # 获取发起人的微信id
            reviewman_wechat = users.objects.get(username=workflowDetail.engineer)
            wechat_engineer_userid = reviewman_wechat.outer_id
            wechat_userid = []
            wechat_userid.append(wechat_engineer_userid)

            # 获取审核人的微信id
            reviewman_wechat = users.objects.get(username=listAllReviewMen[0])
            wechat_auditor_userid = reviewman_wechat.outer_id
            wechat_userid.append(wechat_auditor_userid)

            # 获取抄送人微信
            workflowDetaiL = workflow.objects.get(id=workflowId)
            cc_list = json.loads(workflowDetaiL.email_cc)
            for cc_name in cc_list:
                cc_name_list = [outer_id['outer_id'] for outer_id in
                                users.objects.filter(display=str(cc_name)).values('outer_id')]
                for cc_addr in cc_name_list:
                    wechat_userid.append(cc_addr)

            # 发送企业微信
            wechat_userid = list(filter(lambda x: x != '', wechat_userid))

            if wechat_userid:
                wechatTitle = "SQL上线工单执行完毕 # " + str(workflowId)
                Content = "发起人：" + engineer + "\n审核人：" + reviewMen + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus
                wechatContent = "<div class=\"normal\">" + Content  + "</div>"

                # 获取企业微信的access_token
                Token = wechatSender.GetToken("xxx", "xxx")
                message = wechatTitle.encode("utf-8") + '\n'.encode('utf8') + wechatContent.encode("utf-8")
                wechatSender.SendMessage(Token, wechat_userid, wechatTitle.encode("utf-8"), wechatContent.encode("utf-8"), url)


# 给定时任务执行sql
def execute_job(workflowId, url):
    job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
    logger.debug('execute_job:' + job_id + ' start')
    workflowDetail = workflow.objects.get(id=workflowId)
    clusterName = workflowDetail.cluster_name

    # 服务器端二次验证，当前工单状态必须为定时执行过状态
    if workflowDetail.status != Const.workflowStatus['timingtask']:
        raise Exception('工单不是定时执行状态')

    # 将流程状态修改为执行中，并更新reviewok_time字段
    workflowDetail.status = Const.workflowStatus['executing']
    workflowDetail.reviewok_time = timezone.now()
    try:
        workflowDetail.save()
    except Exception:
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()
    logger.debug('execute_job:' + job_id + ' executing')
    # 执行之前重新split并check一遍，更新SHA1缓存；因为如果在执行中，其他进程去做这一步操作的话，会导致inception core dump挂掉
    splitReviewResult = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name,
                                                   isSplit='yes')
    workflowDetail.review_content = json.dumps(splitReviewResult)
    try:
        workflowDetail.save()
    except Exception:
        # 关闭后重新获取连接，防止超时
        connection.close()
        workflowDetail.save()

    # 采取异步回调的方式执行语句，防止出现持续执行中的异常
    t = Thread(target=execute_call_back, args=(workflowId, clusterName, url))
    t.start()

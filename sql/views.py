# -*- coding: UTF-8 -*-
import datetime
import re
import simplejson as json
from threading import Thread
from collections import OrderedDict

from django.db.models import Q
from django.db import connection, transaction
from django.utils import timezone
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse

from sql.jobs import job_info, add_sqlcronjob, del_sqlcronjob
from sql.sqlreview import getDetailUrl, execute_call_back
from .dao import Dao
from .const import Const, WorkflowDict
from .sendmail import MailSender
from .sendwechat import WechatSender
from .inception import InceptionDao
from .aes_decryptor import Prpcrypt
from .models import users, master_config, AliyunRdsConfig, workflow, slave_config, QueryPrivileges, QueryPrivilegesApply, datasource
from .workflow import Workflow
from .permission import role_required, superuser_required
import logging

logger = logging.getLogger('default')

dao = Dao()
inceptionDao = InceptionDao()
mailSender = MailSender()
wechatSender=WechatSender()
prpCryptor = Prpcrypt()
workflowOb = Workflow()


# 登录
def login(request):
    return render(request, 'login.html')


# 退出登录
def logout(request):
    if request.session.get('login_username', False):
        del request.session['login_username']
    return HttpResponseRedirect(reverse('sql:login'))


# SQL上线工单页面
def sqlworkflow(request):
    context = {'currentMenu': 'allworkflow'}
    return render(request, 'sqlworkflow.html', context)


# 提交SQL的页面
def submitSql(request):
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')

    # 获取所有集群名称
    listAllClusterName = [master.cluster_name for master in masters]

    dictAllClusterDb = OrderedDict()

    # 加入是否是生产环境标志位
    is_product_mark = {}
    for master in masters:
        is_product_mark[master.cluster_name] = master.is_product

    for clusterName in listAllClusterName:
        try:
            dictAllClusterDb[clusterName] = is_product_mark[clusterName]
        except Exception as msg:
            dictAllClusterDb[clusterName] = [str(msg)]

    # 获取所有审核人，当前登录用户不可以审核
    loginUser = request.session.get('login_username', False)
    reviewMen_auditor = users.objects.filter(role__in=['审核人'])
    reviewMen_DBA = users.objects.filter(role__in=['DBA'])
    
    workflowid = request.POST.get('workflowid')
    if not workflowid:
        Workflow = workflow()

    context = {'currentMenu': 'allworkflow', 'dictAllClusterDb': dictAllClusterDb, 'reviewMen_auditor': reviewMen_auditor, 'reviewMen_DBA': reviewMen_DBA, 'Workflow': Workflow}
    return render(request, 'submitSql.html', context)


# 提交SQL给inception进行解析
def autoreview(request):
    workflowid = request.POST.get('workflowid')
    sqlContent = request.POST['sql_content']
    workflowName = request.POST['workflow_name']
    clusterName = request.POST['cluster_name']
    isBackup = request.POST['is_backup']
    is_data_modified = request.POST['is_data_modified']

    reviewAuditor = request.POST.get('review_man_auditor', '')
    reviewDBA = request.POST.get('review_man_DBA', '')

    # 获取邮件抄送人
    email_cc_source = request.POST.get('email_cc_list', '')
    email_cc = []
    for cc in email_cc_source.split(','):
        email_cc.append(cc.strip())

    if len(reviewAuditor) > 0:
        reviewMan = reviewAuditor
        listAllReviewMen = (reviewMan, )
    if len(reviewDBA) > 0:
        subReviewMen = reviewDBA
        listAllReviewMen = (subReviewMen, )
    if len(reviewAuditor) == 0 and len(reviewDBA) == 0:
        listAllReviewMen = ()


    # 服务器端参数验证
    if sqlContent is None or workflowName is None or clusterName is None or isBackup is None or reviewAuditor is None or reviewDBA is None:
        context = {'errMsg': '页面提交参数可能为空'}
        return render(request, 'error.html', context)
    sqlContent = sqlContent.rstrip()
    if sqlContent[-1] != ";":
        context = {'errMsg': "SQL语句结尾没有以;结尾，请后退重新修改并提交！"}
        return render(request, 'error.html', context)

    # 交给inception进行自动审核
    try:
        result = inceptionDao.sqlautoReview(sqlContent, clusterName)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    if result is None or len(result) == 0:
        context = {'errMsg': 'inception返回的结果集为空！可能是SQL语句有语法错误'}
        return render(request, 'error.html', context)
    # 要把result转成JSON存进数据库里，方便SQL单子详细信息展示
    jsonResult = json.dumps(result)

    # 遍历result，看是否有任何自动审核不通过的地方，一旦有，则为自动审核不通过；没有的话，则为等待人工审核状态
    workflowStatus = Const.workflowStatus['manreviewing']
    for row in result:
        if row[2] == 2:
            # 状态为2表示严重错误，必须修改
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break
        elif re.match(r"\w*comments\w*", row[4]):
            workflowStatus = Const.workflowStatus['autoreviewwrong']
            break

    # 存进数据库里
    engineer = request.session.get('login_username', False)
    if not workflowid:
        Workflow = workflow()
        Workflow.create_time = timezone.now()
    else:
        Workflow = workflow.objects.get(id=int(workflowid))
    Workflow.workflow_name = workflowName
    Workflow.engineer = engineer
    Workflow.review_man = json.dumps(listAllReviewMen, ensure_ascii=False)

    # 对于测试、beta环境的数据订正，取消审核人
    if len(reviewAuditor) == 0 and len(reviewDBA) == 0:
        workflowStatus = Const.workflowStatus['pass']
        Workflow.status = workflowStatus
    else:
        Workflow.status = workflowStatus

    Workflow.is_backup = isBackup
    Workflow.is_data_modified = is_data_modified
    Workflow.review_content = jsonResult
    Workflow.cluster_name = clusterName
    Workflow.sql_content = sqlContent
    Workflow.execute_result = ''
    Workflow.audit_remark = ''
    Workflow.email_cc = json.dumps(email_cc, ensure_ascii=False)
    Workflow.save()
    workflowId = Workflow.id

    # 获取审核人的微信id
    if listAllReviewMen:
        reviewman_wechat = users.objects.get(username=listAllReviewMen[0])
        wechat_auditor_userid = reviewman_wechat.outer_id
        wechat_userid = []
        wechat_userid.append(wechat_auditor_userid)
    else:
        wechat_userid = False

    # 获取抄送人微信
    if wechat_userid != False:
        workflowDetaiL = workflow.objects.get(id=workflowId)
        cc_list = json.loads(workflowDetaiL.email_cc)
        for cc_name in cc_list:
            cc_name_list = [outer_id['outer_id'] for outer_id in
                           users.objects.filter(display=str(cc_name)).values('outer_id')]
            for cc_addr in cc_name_list:
                wechat_userid.append(cc_addr)
    # 获取所有DBA微信
    if wechat_userid != False:
        allDBA = users.objects.filter(role='DBA')
        for dba in allDBA:
            wechat_userid.append(dba.outer_id)

    # 自动审核通过了，才发邮件
    if workflowStatus == Const.workflowStatus['manreviewing']:
        # 如果进入等待人工审核状态了，则根据settings.py里的配置决定是否给审核人发一封邮件提醒.
        if hasattr(settings, 'MAIL_ON_OFF') == True:
            if getattr(settings, 'MAIL_ON_OFF') == "on":
                url_origin = getDetailUrl(request) + str(workflowId) + '/'
                url = url_origin.replace('127.0.0.1:8000', '172.31.0.68')

                # 发一封邮件
                strTitle = "新的SQL上线工单提醒 # " + str(workflowId)
                strContent = "发起人：" + engineer + "\n审核人：" + str(
                    listAllReviewMen) + "\n工单环境："+ "生产环境" + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n具体SQL：" + sqlContent
                reviewManAddr = [email['email'] for email in
                                 users.objects.filter(username__in=listAllReviewMen).values('email')]

                # 获取抄送人邮件地址
                workflowDetaiL = workflow.objects.get(id=workflowId)
                cc_list=json.loads(workflowDetaiL.email_cc)
                for cc_name in cc_list:
                    cc_name_list = [email['email'] for email in
                                    users.objects.filter(display=str(cc_name)).values('email')]
                    for cc_addr in cc_name_list:
                        reviewManAddr.append(cc_addr)
                mailSender.sendEmail(strTitle, strContent, reviewManAddr)

                # 发送企业微信
                wechat_userid = list(filter(lambda x: x != '', wechat_userid))

                # 发送企业微信
                if wechat_userid:
                    wechatTitle = "新的SQL上线工单提醒 # " + str(workflowId)
                    Content = "发起人：" + engineer + "\n审核人：" + str(
                        listAllReviewMen) + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n具体SQL：" + sqlContent
                    wechatContent = "<div class=\"normal\">" + Content  + "</div>"

                    # 获取企业微信的access_token
                    Token = wechatSender.GetToken("xxx", "xxx")
                    message = wechatTitle.encode("utf-8") + '\n'.encode('utf8') + wechatContent.encode("utf-8")
                    wechatSender.SendMessage(Token, wechat_userid, wechatTitle.encode("utf-8"), wechatContent.encode("utf-8"), url)

            else:
                # 不发邮件
                pass

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 展示SQL工单详细内容，以及可以人工审核，审核通过即可执行
def detail(request, workflowId):
    workflowDetail = get_object_or_404(workflow, pk=workflowId)
    if workflowDetail.status in (Const.workflowStatus['finish'], Const.workflowStatus['exception']) \
            and workflowDetail.is_manual == 0:
        listContent = json.loads(workflowDetail.execute_result)
    else:
        listContent = json.loads(workflowDetail.review_content)
    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
        listDBA = [username['username'] for username in users.objects.filter(role='DBA').values('username')]
    except ValueError:
        listAllReviewMen = (workflowDetail.review_man,)


    # 获取生产数据库的标志位
    clusterName = workflowDetail.cluster_name
    executeDB = master_config.objects.get(cluster_name=clusterName)

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取定时执行任务信息
    if workflowDetail.status == Const.workflowStatus['timingtask']:
        job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
        job = job_info(job_id)
        if job:
            run_date = job.next_run_time
        else:
            run_date = ''
    else:
        run_date = ''

    # sql结果
    column_list = ['ID', 'stage', 'errlevel', 'stagestatus', 'errormessage', 'SQL', 'Affected_rows', 'sequence',
                   'backup_dbname', 'execute_time', 'sqlsha1']
    rows = []
    for row_index, row_item in enumerate(listContent):
        row = {}
        row['ID'] = row_index + 1
        row['stage'] = row_item[1]
        row['errlevel'] = row_item[2]
        row['stagestatus'] = row_item[3]
        row['errormessage'] = row_item[4]
        row['SQL'] = row_item[5]
        row['Affected_rows'] = row_item[6]
        row['sequence'] = row_item[7]
        row['backup_dbname'] = row_item[8]
        row['execute_time'] = row_item[9]
        row['sqlsha1'] = row_item[10]
        rows.append(row)

        if workflowDetail.status == '执行中':
            row['stagestatus'] = ''.join(
                ["<div id=\"td_" + str(row['ID']) + "\" class=\"form-inline\">",
                 "   <div class=\"progress form-group\" style=\"width: 80%; height: 18px; float: left;\">",
                 "       <div id=\"div_" + str(row['ID']) + "\" class=\"progress-bar\" role=\"progressbar\"",
                 "            aria-valuenow=\"60\"",
                 "            aria-valuemin=\"0\" aria-valuemax=\"100\">",
                 "           <span id=\"span_" + str(row['ID']) + "\"></span>",
                 "       </div>",
                 "   </div>",
                 "   <div class=\"form-group\" style=\"width: 10%; height: 18px; float: right;\">",
                 "       <form method=\"post\">",
                 "           <input type=\"hidden\" name=\"workflowid\" value=\"" + str(workflowDetail.id) + "\">",
                 "           <button id=\"btnstop_" + str(row['ID']) + "\" value=\"" + str(row['ID']) + "\"",
                 "                   type=\"button\" class=\"close\" style=\"display: none\" title=\"停止pt-OSC进程\">",
                 "               <span class=\"glyphicons glyphicons-stop\">&times;</span>",
                 "           </button>",
                 "       </form>",
                 "   </div>",
                 "</div>"])
    context = {'currentMenu': 'allworkflow', 'workflowDetail': workflowDetail, 'column_list': column_list, 'rows': rows,
               'listAllReviewMen': listAllReviewMen, 'loginUserOb': loginUserOb, 'run_date': run_date, 'executeDB' : executeDB , 'listDBA' : listDBA}
    return render(request, 'detail.html', context)


# 审核通过，不执行
@role_required(('审核人', 'DBA',))
def passed(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    url_origin = getDetailUrl(request) + str(workflowId) + '/'
    url = url_origin.replace('127.0.0.1:8000', '172.31.0.68')
    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
    except ValueError:
        listAllReviewMen = (workflowDetail.review_man,)

    # 服务器端二次验证，正在执行人工审核动作的当前登录用户必须为审核人. 避免攻击或被接口测试工具强行绕过
    loginUser = request.session.get('login_username', False)
    listDBA = [username['username'] for username in users.objects.filter(role='DBA').values('username')]
    if loginUser not in listDBA:
        if loginUser is None or loginUser not in listAllReviewMen:
            context = {'errMsg': '当前登录用户不是审核人，请重新登录.'}
            return render(request, 'error.html', context)

    # 服务器端二次验证，当前工单状态必须为等待人工审核
    if workflowDetail.status != Const.workflowStatus['manreviewing']:
        context = {'errMsg': '当前工单状态不是等待人工审核中，请刷新当前页面！'}
        return render(request, 'error.html', context)

    # 将流程状态修改为审核通过，并更新reviewok_time字段
    workflowDetail.status = Const.workflowStatus['pass']
    workflowDetail.reviewok_time = timezone.now()
    workflowDetail.audit_remark = ''

    # 更新审核人
    listReviewMen = (loginUser, )
    workflowDetail.review_man = json.dumps(listReviewMen, ensure_ascii=False)
    workflowDetail.save()

    # 获取发起人的微信id
    reviewman_wechat = users.objects.get(username=workflowDetail.engineer)
    wechat_auditor_userid = reviewman_wechat.outer_id
    wechat_userid = []
    if wechat_auditor_userid:
        wechat_userid.append(wechat_auditor_userid)

    # 如果审核通过了了，则根据settings.py里的配置决定是否给提交者和DBA一封邮件提醒.DBA需要知晓审核并执行过的单子
    if hasattr(settings, 'MAIL_ON_OFF') == True:
        if getattr(settings, 'MAIL_ON_OFF') == "on":
            # 给主、副审核人，申请人，DBA各发一封邮件
            engineer = workflowDetail.engineer
            reviewMen = workflowDetail.review_man
            workflowStatus = workflowDetail.status
            workflowName = workflowDetail.workflow_name
            objEngineer = users.objects.get(username=engineer)
            strTitle = "SQL上线工单审核通过 # " + str(workflowId)
            strContent = "发起人：" + engineer + "\n审核人：" + reviewMen + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n审核结果：" + workflowStatus
            reviewManAddr = [email['email'] for email in
                             users.objects.filter(username__in=listAllReviewMen).values('email')]
            dbaAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
            listCcAddr = reviewManAddr + dbaAddr
            mailSender.sendEmail(strTitle, strContent, [objEngineer.email], listCcAddr=listCcAddr)

            # 发送企业微信
            wechat_userid = list(filter(lambda x: x != '', wechat_userid))

            if wechat_userid:
                wechatTitle = "SQL上线工单审核通过 # " + str(workflowId)
                Content = "发起人：" + engineer + "\n审核人：" + reviewMen + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n审核结果：" + workflowStatus
                wechatContent = "<div class=\"normal\">" + Content  + "</div>"

                # 获取企业微信的access_token
                Token = wechatSender.GetToken("xxx", "xxx")
                message = wechatTitle.encode("utf-8") + '\n'.encode('utf8') + wechatContent.encode("utf-8")
                wechatSender.SendMessage(Token, wechat_userid, wechatTitle.encode("utf-8"), wechatContent.encode("utf-8"), url)

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 执行SQL
@role_required(('工程师','DBA'))
def execute(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    # 获取审核人
    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
    except ValueError:
        listAllReviewMen = (workflowDetail.review_man,)


    clusterName = workflowDetail.cluster_name
    url_origin = getDetailUrl(request) + str(workflowId) + '/'
    url = url_origin.replace('127.0.0.1:8000', '172.31.0.68')

    # 服务器端二次验证，当前工单状态必须为审核通过状态
    if workflowDetail.status != Const.workflowStatus['pass']:
        context = {'errMsg': '当前工单状态不是审核通过，请刷新当前页面！'}
        return render(request, 'error.html', context)


    # 获取生产数据库的标志位
    executeDB = master_config.objects.get(cluster_name=clusterName)


    # 对于生产环境，不真正执行，采用手工方式执行，只是更新标准位
    if executeDB.is_product == 1:
        try:
            workflowDetail.status='已正常结束'
            workflowDetail.execute_result=workflowDetail.review_content.replace('Audit completed', 'Execute Successfully')
            workflowDetail.save()

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
            dbaAddr = [email['email'] for email in users.objects.filter(role='DBA').values('email')]
            # 获取抄送人邮件地址
            workflowDetaiL = workflow.objects.get(id=workflowId)
            cc_list = json.loads(workflowDetaiL.email_cc)
            for cc_name in cc_list:
                cc_name_list = [email['email'] for email in
                                users.objects.filter(display=str(cc_name)).values('email')]
                for cc_addr in cc_name_list:
                    reviewManAddr.append(cc_addr)
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


        except Exception:
        # 关闭后重新获取连接，防止超时
            connection.close()
            workflowDetail.save()

    elif executeDB.is_product == 0:

            # 将流程状态修改为执行中，并更新reviewok_time字段
            workflowDetail.status = Const.workflowStatus['executing']
            workflowDetail.reviewok_time = timezone.now()
            # 执行之前重新split并check一遍，更新SHA1缓存；因为如果在执行中，其他进程去做这一步操作的话，会导致inception core dump挂掉
            try:
                splitReviewResult = inceptionDao.sqlautoReview(workflowDetail.sql_content, workflowDetail.cluster_name,
                                                           isSplit='yes')
            except Exception as msg:
                context = {'errMsg': msg}
                return render(request, 'error.html', context)
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

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 定时执行SQL
@role_required(('DBA',))
def timingtask(request):
    workflowId = request.POST.get('workflowid')
    run_date = request.POST.get('run_date')
    if run_date is None or workflowId is None:
        context = {'errMsg': '时间不能为空'}
        return render(request, 'error.html', context)
    elif run_date < datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'):
        context = {'errMsg': '时间不能小于当前时间'}
        return render(request, 'error.html', context)
    workflowDetail = workflow.objects.get(id=workflowId)
    if workflowDetail.status not in [Const.workflowStatus['pass'], Const.workflowStatus['timingtask']]:
        context = {'errMsg': '必须为审核通过或者定时执行状态'}
        return render(request, 'error.html', context)

    run_date = datetime.datetime.strptime(run_date, "%Y-%m-%d %H:%M:%S")
    url = getDetailUrl(request) + str(workflowId) + '/'
    job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)

    # 使用事务保持数据一致性
    try:
        with transaction.atomic():
            # 将流程状态修改为定时执行
            workflowDetail.status = Const.workflowStatus['timingtask']
            workflowDetail.save()
            # 调用添加定时任务
            add_sqlcronjob(job_id, run_date, workflowId, url)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 终止流程
def cancel(request):
    workflowId = request.POST['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)

    workflowId = int(workflowId)
    workflowDetail = workflow.objects.get(id=workflowId)
    reviewMan = workflowDetail.review_man
    try:
        listAllReviewMen = json.loads(reviewMan)
    except ValueError:
        listAllReviewMen = (reviewMan,)

    audit_remark = request.POST.get('audit_remark')
    if audit_remark is None:
        context = {'errMsg': '驳回原因不能为空'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果正在执行终止动作的当前登录用户，不是发起人也不是审核人，并且不是DBA，则异常.
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    if loginUser not in listAllReviewMen and loginUser != workflowDetail.engineer and loginUserOb.role != 'DBA':
        context = {'errMsg': '当前登录用户不是审核人也不是发起人，请重新登录.'}
        return render(request, 'error.html', context)

    # 服务器端二次验证，如果当前单子状态是结束状态，则不能发起终止
    if workflowDetail.status in (
            Const.workflowStatus['abort'], Const.workflowStatus['finish'], Const.workflowStatus['autoreviewwrong'],
            Const.workflowStatus['exception']):
        return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))

    # 删除定时执行job
    if workflowDetail.status == Const.workflowStatus['timingtask']:
        job_id = Const.workflowJobprefix['sqlreview'] + '-' + str(workflowId)
        del_sqlcronjob(job_id)
    # 将流程状态修改为人工终止流程
    workflowDetail.status = Const.workflowStatus['abort']
    workflowDetail.audit_remark = audit_remark
    workflowDetail.save()

    # 如果人工终止了，则根据settings.py里的配置决定是否给提交者和审核人发邮件提醒。如果是发起人终止流程，则给主、副审核人各发一封；如果是审核人终止流程，则给发起人发一封邮件，并附带说明此单子被拒绝掉了，需要重新修改.
    if hasattr(settings, 'MAIL_ON_OFF') == True:
        if getattr(settings, 'MAIL_ON_OFF') == "on":
            url_origin = getDetailUrl(request) + str(workflowId) + '/'
            url = url_origin.replace('127.0.0.1:8000', '172.31.0.68')

            engineer = workflowDetail.engineer
            workflowStatus = workflowDetail.status
            workflowName = workflowDetail.workflow_name
            if loginUser == engineer:
                strTitle = "发起人主动终止SQL上线工单流程 # " + str(workflowId)
                strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus + "\n提醒：发起人主动终止流程"
                reviewManAddr = [email['email'] for email in
                                 users.objects.filter(username__in=listAllReviewMen).values('email')]
                mailSender.sendEmail(strTitle, strContent, reviewManAddr)

                # 获取审核人的微信id
                if listAllReviewMen:
                    reviewman_wechat = users.objects.get(username=listAllReviewMen[0])
                    wechat_auditor_userid = reviewman_wechat.outer_id
                    wechat_userid = []
                    wechat_userid.append(wechat_auditor_userid)
                else:
                    wechat_userid = False

                # 发送企业微信
                wechat_userid = list(filter(lambda x: x != '', wechat_userid))

                if wechat_userid:
                    wechatTitle = "发起人主动终止SQL上线工单流程 # " + str(workflowId)
                    Content = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus + "\n提醒：发起人主动终止流程"
                    wechatContent = "<div class=\"normal\">" + Content  + "</div>"

                    # 获取企业微信的access_token
                    Token = wechatSender.GetToken("xxx", "xxx")
                    message = wechatTitle.encode("utf-8") + '\n'.encode('utf8') + wechatContent.encode("utf-8")
                    wechatSender.SendMessage(Token, wechat_userid, wechatTitle.encode("utf-8"), wechatContent.encode("utf-8"), url)

            else:
                objEngineer = users.objects.get(username=engineer)
                strTitle = "SQL上线工单被拒绝执行 # " + str(workflowId)
                strContent = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus + "\n提醒：此工单被拒绝执行，请登陆重新提交或修改工单"
                mailSender.sendEmail(strTitle, strContent, [objEngineer.email])

                # 获取审核人的微信id
                reviewman_wechat = users.objects.get(username=workflowDetail.engineer)
                wechat_auditor_userid = reviewman_wechat.outer_id
                wechat_userid = []
                wechat_userid.append(wechat_auditor_userid)


                # 发送企业微信
                wechat_userid = list(filter(lambda x: x != '', wechat_userid))

                if wechat_userid:
                    wechatTitle = "SQL上线工单被拒绝执行 # " + str(workflowId)
                    Content = "发起人：" + engineer + "\n审核人：" + reviewMan + "\n工单地址：" + url + "\n工单名称： " + workflowName + "\n执行结果：" + workflowStatus + "\n提醒：此工单被拒绝执行，请登陆重新提交或修改工单"
                    wechatContent = "<div class=\"normal\">" + Content  + "</div>"

                    # 获取企业微信的access_token
                    Token = wechatSender.GetToken("xxx", "xxx")
                    message = wechatTitle.encode("utf-8") + '\n'.encode('utf8') + wechatContent.encode("utf-8")
                    wechatSender.SendMessage(Token, wechat_userid, wechatTitle.encode("utf-8"), wechatContent.encode("utf-8"), url)

        else:
            # 不发邮件
            pass

    return HttpResponseRedirect(reverse('sql:detail', args=(workflowId,)))


# 展示回滚的SQL
def rollback(request):
    workflowId = request.GET['workflowid']
    if workflowId == '' or workflowId is None:
        context = {'errMsg': 'workflowId参数为空.'}
        return render(request, 'error.html', context)
    workflowId = int(workflowId)
    try:
        listBackupSql = inceptionDao.getRollbackSqlList(workflowId)
    except Exception as msg:
        context = {'errMsg': msg}
        return render(request, 'error.html', context)
    workflowDetail = workflow.objects.get(id=workflowId)
    workflowName = workflowDetail.workflow_name
    rollbackWorkflowName = "【回滚工单】原工单Id:%s ,%s" % (workflowId, workflowName)
    cluster_name = workflowDetail.cluster_name


    try:
        listAllReviewMen = json.loads(workflowDetail.review_man)
        if listAllReviewMen:
            review_man = listAllReviewMen[0]
            # sub_review_man = listAllReviewMen[1]
        else:
            review_man = ''
    except ValueError:
        review_man = workflowDetail.review_man
        sub_review_man = ''

    context = {'listBackupSql': listBackupSql, 'rollbackWorkflowName': rollbackWorkflowName,
               'cluster_name': cluster_name, 'review_man': review_man }
    return render(request, 'rollback.html', context)


# SQL审核必读
def dbaprinciples(request):
    context = {'currentMenu': 'dbaprinciples'}
    return render(request, 'dbaprinciples.html', context)


# 图表展示
def charts(request):
    context = {'currentMenu': 'charts'}
    return render(request, 'charts.html', context)

# 数据源管理
def managedatasource(request):

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    context = {'currentMenu': 'alldatasource', 'loginUserOb': loginUserOb}
    return render(request, 'datasource.html', context)

# 增加数据源
def adddatasource(request):

    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    context = {'loginUserOb': loginUserOb}
    return render(request, 'adddatasource.html', context)

# SQL在线查询
def sqlquery(request):
    # 获取所有从库集群名称
    slaves = slave_config.objects.all().order_by('cluster_name')
    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')
    listAllClusterName = [slave.cluster_name for slave in slaves]

    context = {'currentMenu': 'sqlquery', 'listAllClusterName': listAllClusterName}
    return render(request, 'sqlquery.html', context)


# SQL慢日志
def slowquery(request):
    # 获取所有集群主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'slowquery', 'tab': 'slowquery', 'cluster_name_list': cluster_name_list}
    return render(request, 'slowquery.html', context)


# SQL优化工具
def sqladvisor(request):
    # 获取所有集群主库名称
    masters = master_config.objects.all().order_by('cluster_name')
    if len(masters) == 0:
        return HttpResponseRedirect('/admin/sql/master_config/add/')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'sqladvisor', 'listAllClusterName': cluster_name_list}
    return render(request, 'sqladvisor.html', context)


def queryapplylist(request):
    slaves = slave_config.objects.all().order_by('cluster_name')
    # 获取所有实例从库名称
    listAllClusterName = [slave.cluster_name for slave in slaves]
    dictAllClusterDb = OrderedDict()

    if len(slaves) == 0:
        return HttpResponseRedirect('/admin/sql/slave_config/add/')

    # 加入是否是生产环境标志位
    is_product_mark = {}
    for slave in slaves:
        is_product_mark[slave.cluster_name] = slave.is_product

    # 每一个都首先获取主库地址在哪里
    for clusterName in listAllClusterName:
        dictAllClusterDb[clusterName] = is_product_mark[clusterName]

    # 获取当前审核人信息
    auditors = workflowOb.auditsettings(workflow_type=WorkflowDict.workflow_type['query'])

    # 获取生产数据库的标志位
    # executeDB = master_config.objects.get(cluster_name=clusterName)

    # 对于生产环境，不真正执行，采用手工方式执行，只是更新标准位
    # if executeDB.is_product == 1:

    context = {'currentMenu': 'queryapply', 'listAllClusterName': listAllClusterName,
                   'auditors': auditors, 'dictAllClusterDb': dictAllClusterDb}

    return render(request, 'queryapplylist.html', context)


# 查询权限申请详情
def queryapplydetail(request, apply_id):
    workflowDetail = QueryPrivilegesApply.objects.get(apply_id=apply_id)
    # 获取当前审核人
    audit_info = workflowOb.auditinfobyworkflow_id(workflow_id=apply_id,
                                                   workflow_type=WorkflowDict.workflow_type['query'])

    context = {'currentMenu': 'queryapply', 'workflowDetail': workflowDetail, 'audit_info': audit_info}
    return render(request, 'queryapplydetail.html', context)


# 用户的查询权限管理
def queryuserprivileges(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    # 获取所有用户
    user_list = QueryPrivileges.objects.filter(is_deleted=0).values('user_name').distinct()
    context = {'currentMenu': 'queryapply', 'user_list': user_list, 'loginUserOb': loginUserOb}
    return render(request, 'queryuserprivileges.html', context)


# 问题诊断--进程
def diagnosis_process(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)

    # 获取所有实例名称
    masters = master_config.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'process', 'cluster_name_list': cluster_name_list,
               'loginUserOb': loginUserOb}
    return render(request, 'diagnosis.html', context)


# 问题诊断--空间
def diagnosis_sapce(request):
    # 获取所有集群名称
    masters = AliyunRdsConfig.objects.all().order_by('cluster_name')
    cluster_name_list = [master.cluster_name for master in masters]

    context = {'currentMenu': 'diagnosis', 'tab': 'space', 'cluster_name_list': cluster_name_list}
    return render(request, 'diagnosis.html', context)


# 获取工作流审核列表
def workflows(request):
    # 获取用户信息
    loginUser = request.session.get('login_username', False)
    loginUserOb = users.objects.get(username=loginUser)
    context = {'currentMenu': 'workflow', "loginUserOb": loginUserOb}
    return render(request, "workflow.html", context)


# 工作流审核列表
def workflowsdetail(request, audit_id):
    # 按照不同的workflow_type返回不同的详情
    auditInfo = workflowOb.auditinfo(audit_id)
    if auditInfo.workflow_type == WorkflowDict.workflow_type['query']:
        return HttpResponseRedirect(reverse('sql:queryapplydetail', args=(auditInfo.workflow_id,)))

# 增加数据源
def autodatasource(request):

    App_name = request.POST.get("app_name", "")
    Env = request.POST.get("env", "")
    Db_name = request.POST.get("db_name", "")
    Ip_addr = request.POST.get("ip_addr", "")
    Port = request.POST.get("port", "")
    Username = request.POST.get("username", "")
    Password = request.POST.get("password", "")

    if len(App_name) > 0 and len(Env) > 0 and len(Db_name) > 0 and len(Ip_addr) > 0 and len(Port) > 0 and len(Username) > 0 and len(Password) > 0:
        D1 = datasource()
        D1.app_name = App_name
        D1.env = Env
        D1.db_name = Db_name
        D1.ip_addr = Ip_addr
        D1.port = Port
        D1.username = Username
        D1.password = Password
        D1.save()

    return HttpResponseRedirect(reverse('sql:Datasource'))

# 修改数据源
def modifydatasource(request):

    Datasourceid = request.POST.get("datasourceid", "")
    App_name = request.POST.get("app_name", "")
    Env = request.POST.get("env", "")
    Db_name = request.POST.get("db_name", "")
    Ip_addr = request.POST.get("ip_addr", "")
    Port = request.POST.get("port", "")
    Username = request.POST.get("username", "")
    Password = request.POST.get("password", "")
    if len(Datasourceid) > 0 and  len(App_name) > 0 and len(Env) > 0 and len(Db_name) > 0 and len(Ip_addr) > 0 and len(Port) > 0 and len(Username) > 0 and len(Password) > 0:
        D1 = datasource(pk=Datasourceid)
        D1.app_name = App_name
        D1.env = Env
        D1.db_name = Db_name
        D1.ip_addr = Ip_addr
        D1.port = Port
        D1.username = Username
        D1.password = Password
        D1.save()

    return HttpResponseRedirect(reverse('sql:Datasource'))


# 展示数据源详细内容
def datasourcedetail(request, datasourceId):
    datasourceDetail = get_object_or_404(datasource, pk=datasourceId)

    # 获取用户信息
    loginUser = request.session.get('login_username', False)

    # 获取搜索参数
    search = request.POST.get('search')
    if search is None:
        search = ''

    # 管理员可以看到全部工单，其他人能看到自己提交和审核的工单
    loginUserOb = users.objects.get(username=loginUser)

    # 全部工单里面包含搜索条件,待审核前置
    listDatasource = datasource.objects.filter(
        Q(db_name__contains=search) | Q(ip_addr__contains=search)  | Q(env__contains=search) | Q(app_name__contains=search) | Q(username__contains=search)
        ).order_by('-id').values("id", "app_name", "env", "db_name",
                                                    "ip_addr", "port", "username", "password")
    listDatasourceCount = datasource.objects.filter(
        Q(db_name__contains=search) | Q(ip_addr__contains=search)).count()

    # QuerySet 序列化
    rows = [row for row in listDatasource]

    context = {'datasourceDetail': datasourceDetail, "rows": rows, 'loginUserOb': loginUserOb}

    return render(request, 'datasourcedetail.html', context)

# 用户修改密码
def passwordchange(request):
	return render(request, 'passwordchange.html')
	
# 用户修改密码
def autopassword(request):

    password = request.POST.get("password", "")
    password_repeat = request.POST.get("password_repeat", "")
    loginUser = request.session.get('login_username', False)

    if password == password_repeat and len(password) > 0 and len(password_repeat) > 0:
        u = users.objects.get(username=loginUser)
        u.set_password(password)
        u.save()

    return HttpResponseRedirect(reverse('sql:login'))

# 数据库运维
def maintenance(request):
    context = {'currentMenu': 'maintenance'}
    return render(request, 'maintenance.html', context)

# 备份关联
def dbbackup(request):
    context = {'currentMenu': 'dbbackup'}
    return render(request, 'dbbackup.html', context)

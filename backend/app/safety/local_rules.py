"""Small deterministic baseline for offline use, not a semantic safety classifier."""

import html
import re
import unicodedata

from app.safety.policy import CATEGORIES


def normalize(value):
    value = html.unescape(unicodedata.normalize("NFKC", value)).casefold()
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    return value


def matches(pattern, text):
    return re.search(pattern, text, re.I) is not None


def classify(text):
    scores = dict.fromkeys(CATEGORIES, 0.0)
    value = normalize(text)
    # Classify clauses independently. An educational prefix must not exempt a later harmful request.
    for clause in re.split(r"[。！？!?;；\n]|[，,](?=(?:请|教|帮|然后|但|并|如何|给我))", value):
        clause = re.sub(r"^(?:用户|助手|当前问题|历史用户|历史助手)\s*[:：]\s*", "", clause.strip())
        if not clause.strip():
            continue
        request = matches(
            r"教我|帮我|给我|告诉我|如何|怎么|怎样|写(?:一|个|段|封|出)|生成|提供|制作|制造|策划|步骤|教程|指南|how\s+(?:do\s+i|to|can\s+i)|write|create|generate|make|build|instructions|step.by.step|help\s+me",
            clause,
        )
        benign = matches(
            r"^(?:请)?(?:如何|怎么|怎样)?(?:预防|防止|识别|检测|防范|阻止|劝阻|举报|避免|治疗|帮助)|^(?:how\s+(?:to|can\s+i)\s+)?(?:prevent|detect|report|avoid|help\s+someone)|^(?:我)?(?:不能|无法|不会|不应|不要)|^i\s+(?:cannot|can't|won't)|^(?:do\s+not|don't)",
            clause,
        )
        explicit = matches(
            r"教我|给我.{0,12}(?:步骤|教程|方法)|提供.{0,12}(?:步骤|教程)|step.by.step|instructions\s+(?:for|to)",
            clause,
        )
        if benign and not explicit:
            continue
        if request and matches(
            r"(?:制作|制造|组装).{0,15}(?:炸弹(?!量热计)|爆炸装置)|(?:杀人|杀死(?:某人|别人|他|她|同事)|杀害|暗杀|投毒|恐怖袭击)|(?:make|build|assemble).{0,20}(?:a\s+)?bomb(?!\s+calorimeter)|(?:murder|assassinat|poison\s+(?:someone|my)|kill\s+(?:someone|a\s+person))",
            clause,
        ):
            scores["violence"] = 1.0
        if matches(
            r"我要(?:杀死|杀了|枪杀)|i\s+(?:will|am\s+going\s+to)\s+kill\s+(?:you|him|her|them)", clause
        ):
            scores["violence"] = 1.0
        if request and matches(
            r"仇恨宣传|煽动.{0,18}(?:屠杀|驱逐|迫害)|宣扬.{0,18}(?:种族优越|低等种族)|hate\s+propaganda|incite.{0,25}(?:genocide|racial\s+hatred)",
            clause,
        ):
            scores["hate"] = 1.0
        if request and matches(
            r"网暴|人肉搜索|威胁信|骚扰(?:她|他|别人|同事)|羞辱(?:她|他|同事)|harass\s+(?:her|him|my)|threatening\s+(?:letter|message)|dox(?:x)?\s+(?:her|him|someone)",
            clause,
        ):
            scores["harassment"] = 1.0
        minor = matches(
            r"未成年|儿童|幼童|小学生|(?:1[0-7]|[0-9])岁|\b(?:child|children|underage|minor|(?:1[0-7]|[0-9])[ -]year[ -]old)\b",
            clause,
        )
        sexual = matches(
            r"色情|性描写|性爱|裸照|性行为|露骨|porn|erotic|sexually\s+explicit|nude\s+(?:photo|image)",
            clause,
        )
        if sexual and minor:
            scores["sexual_minors"] = 1.0
        if (
            request
            and sexual
            and matches(
                r"色情|露骨.{0,12}(?:性|描写)|性爱.{0,12}(?:细节|描写)|porn|erotic|sexually\s+explicit",
                clause,
            )
        ):
            scores["sexual_explicit"] = 1.0
        if matches(
            r"(?:我想|我要|我准备|不想活).{0,10}(?:自杀|自残|割腕)|(?:自杀|自残).{0,10}(?:方法|教程|步骤)|(?:如何|怎么|教我).{0,8}(?:自杀|自残)|(?:kill\s+myself|end\s+my\s+life)|(?:how\s+to|instructions\s+for).{0,12}(?:suicide|self.harm)",
            clause,
        ):
            scores["self_harm"] = 1.0
        if request and matches(
            r"(?:制作|制造|合成).{0,10}(?:毒品|冰毒)|(?:诈骗|洗钱).{0,10}(?:教程|步骤|话术|方案)|骗(?:别人|他人|人).{0,8}(?:转账|钱)|逃避.{0,8}(?:警方|侦查)|(?:make|manufacture).{0,15}(?:methamphetamine|illegal\s+drugs)|money\s+laundering\s+(?:steps|instructions)|scam\s+script",
            clause,
        ):
            scores["illicit_instructions"] = 1.0
        if request and matches(
            r"窃取.{0,16}(?:密码|凭据|令牌|cookie)|盗取.{0,16}(?:账号|密码)|(?:黑进|入侵).{0,6}(?:别人|他人|邻居).{0,8}(?:电脑|账号|网络)|(?:勒索软件|钓鱼邮件).{0,12}(?:代码|脚本|模板)|(?:编写|生成|制作).{0,12}(?:勒索软件|钓鱼邮件)|steal.{0,20}(?:password|credential|cookie)|hack.{0,12}(?:neighbor|someone.else)|(?:ransomware|phishing)\s+(?:code|script|email)",
            clause,
        ):
            scores["cyber_abuse"] = 1.0
    return scores

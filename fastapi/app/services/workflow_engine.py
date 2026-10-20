"""
命令工作流执行引擎

节点类型与参数：
- llm:            {prompt(模板), temperature?, max_tokens?}   → 调用绑定智能体的 LLM
- table_query:    {table, where?, limit?}                     → 查表（只读）
- table_insert:   {table, row}                                → 插入（需写权限）
- table_update:   {table, id, fields}                         → 更新（需写权限）
- table_delete:   {table, id}                                 → 删除（需写权限）
- table_create:   {name, description?, fields}                → 创建表格（创建后自动授权绑定智能体读写）
- http_get:       {url, headers?}                             → GET 联网
- http_post:      {url, data?, headers?}                      → POST 联网
- device_command: {cmd_type, params?, expect_type?, wait_sec?} → 设备指令
- task_create:   {name, trigger_type, trigger_spec, action_type, action_spec, robot_id?, org_id?, enabled?} → 创建任务
- condition:      {field, op, value}                          → 条件判断（满足→成功分支）
- switch:         {field, branches[], default_next}           → 多分支判断（分支/兜底都必须显式指定目标，留空=中止）
- code:           {code}                                      → 运行 Python 代码（output 变量为结果）
- output:         {text(模板)}                                → 最终回复文本

数据流：
- ctx["input"] = 输入上下文 {robot_id, room_name, room_type, user_text, sender}
- 每节点输出存 ctx[节点名]，后续节点用 {{节点名.字段}} 引用（支持数字索引）
- 节点成功 → next_on_success（空=顺序下一节点，__end__=到此结束）；失败 → next_on_fail（空=中止）
- switch 命中分支 → 该分支 next（空=中止）；全不命中 → default_next（空=中止）
"""
import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from ..models.workflow import END_TARGET
from .workflow_service import workflow_service
from .agent_service import agent_service
from .skill_service import skill_service
from .group_service import group_service
from .table_service import table_service
from .knowledge_service import knowledge_service
from .aivbot_service import build_command_extra, aivbot_service

logger = logging.getLogger(__name__)

_TMPL_RE = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")

# 设备获取类指令 → received_data 存储 key
_EXPECT_KEYS = {
    "501": "group_info_detail",
    "502": "friend_info",
    "503": "my_info",
    "505": "recent_list",
    "506": "corp_list",
    "507": "contacts",
    "508": "groups",
}

# 获取类指令 cmd_type → received_data 存储 key（expect_type 为空时按指令自动等待数据上报；
# 501/503/505/506 设备只上报数据、不发执行回执，等 last_result 会超时）
_CMD_DATA_KEYS = {
    501: "group_info_detail",
    502: "friend_info",
    503: "my_info",
    505: "recent_list",
    506: "corp_list",
    507: "contacts",
    508: "groups",
}


class WorkflowEngine:
    """命令工作流执行引擎（单次 execute 运行一条工作流）"""

    async def execute(
        self,
        workflow_id: int,
        robot_id: str,
        input_ctx: dict,
        wait_device: bool = True,
        simulate: bool = False,
        agent=None,
    ) -> dict:
        """
        执行工作流。wait_device=True 时 device_command 节点等待设备回执/上报。
        simulate=True 时 device_command 节点不依赖真实设备，返回虚拟设备数据（测试运行用）。
        agent=执行身份的智能体（LLM/表格节点使用其 LLM 能力与表格授权；None 时这些节点不可用）。
        返回 {status, output, ctx, run_id, error, duration_ms}
        """
        workflow = await workflow_service.get_workflow(workflow_id)
        if not workflow:
            raise ValueError(f"工作流不存在: {workflow_id}")
        nodes = workflow.get("nodes") or []
        if not nodes:
            raise ValueError(f"工作流「{workflow['name']}」还没有任何节点")

        run_id = await workflow_service.create_run(workflow_id, robot_id, input_ctx)
        # 创建任务节点默认归属当前工作流的组织（节点无需填 org_id，也不可能突破这层限制）
        try:
            wf_org_ids = await group_service.get_member_org_groups("workflow", workflow_id)
            wf_org_id = wf_org_ids[0] if wf_org_ids else 0
        except Exception:
            wf_org_id = 0
        ctx: Dict[str, Any] = {"input": dict(input_ctx or {})}
        ctx["_simulate"] = simulate
        start = time.monotonic()
        timeout_sec = int(workflow.get("timeout_sec") or 60)

        # 纯显式图模型：去向只看每个节点自己写的跳转，**顺序不参与运行语义**。
        # （原来"next_on_success 留空 = 顺序下一个"已经删掉，留空 = 到此结束。）
        # 这里的排序只用来决定"从哪个节点开始"，不再有隐式边。
        ordered = sorted(nodes, key=lambda n: n["id"])
        node_map = {n["name"]: n for n in ordered}

        current = ordered[0]
        visited = set()
        status = "success"
        error = None
        last_output = ""

        while current:
            node = current
            # 循环保护
            if node["name"] in visited:
                status = "loop"
                error = f"检测到节点循环引用（{node['name']}），已终止"
                break
            visited.add(node["name"])
            # 超时保护
            if time.monotonic() - start > timeout_sec:
                status = "timeout"
                error = f"工作流超过 {timeout_sec}s 超时"
                break

            node_start = time.monotonic()
            rendered_params = self._render(node.get("params") or {}, ctx)
            # 任务创建节点：节点未显式指定 org_id 时自动归属当前工作流所属组织
            if node["node_type"] == "task_create" and not rendered_params.get("org_id"):
                rendered_params["org_id"] = wf_org_id
            input_data = {
                "node_type": node["node_type"],
                "params": rendered_params,
            }
            try:
                result, ok = await self._run_node(
                    node["node_type"], rendered_params, ctx, agent
                )
                ctx[node["name"]] = result
                if node["node_type"] == "output":
                    last_output = str(result.get("text") or "")
                node_ms = int((time.monotonic() - node_start) * 1000)
                await workflow_service.log_node(
                    run_id, node["name"], node["node_type"], "success" if ok else "failed",
                    input_data, result, None, node_ms,
                )
                # 多分支节点：命中哪个分支就走哪个（由 params 中的分支定义决定跳转目标）
                if node["node_type"] == "switch":
                    jump = str(result.get("next") or "")
                    if jump == END_TARGET:
                        # 分支显式指向"结束" → 正常收尾
                        current = None
                    elif jump:
                        target = node_map.get(jump)
                        if target is None:
                            status = "failed"
                            error = f"节点「{node['name']}」分支跳转目标「{jump}」不存在，请检查分支配置"
                            break
                        current = target
                    elif result.get("matched"):
                        # 分支必须显式指定跳转目标：留空不能"顺序续下"，否则多条空分支会
                        # 一起续接到 switch 后面那个节点上，谁也说不清新节点属于哪条分支
                        status = "failed"
                        error = (
                            f"节点「{node['name']}」命中的分支「{result.get('matched')}」"
                            "没有指定跳转目标，工作流中止"
                        )
                        break
                    else:
                        status = "failed"
                        error = f"节点「{node['name']}」未命中任何分支且无兜底，工作流中止"
                        break
                    continue
                # 分支控制
                if ok:
                    jump = (node.get("next_on_success") or "").strip()
                    if jump and jump != END_TARGET:
                        target = node_map.get(jump)
                        if target is None:
                            status = "failed"
                            error = f"节点「{node['name']}」成功跳转目标「{jump}」不存在，请检查跳转配置"
                            break
                        current = target
                    else:
                        # 空 或 __end__ → 正常收尾
                        current = None
                else:
                    jump = (node.get("next_on_fail") or "").strip()
                    if jump == END_TARGET:
                        current = None
                    elif jump:
                        target = node_map.get(jump)
                        if target is None:
                            status = "failed"
                            error = f"节点「{node['name']}」失败跳转目标「{jump}」不存在，请检查跳转配置"
                            break
                        current = target
                    else:
                        status = "failed"
                        error = f"节点「{node['name']}」未通过（无失败跳转），工作流中止"
                        break
            except Exception as e:
                logger.warning(f"❌ 工作流节点执行异常: {node['name']}: {e}")
                node_ms = int((time.monotonic() - node_start) * 1000)
                await workflow_service.log_node(
                    run_id, node["name"], node["node_type"], "error",
                    input_data, None, str(e), node_ms,
                )
                jump = (node.get("next_on_fail") or "").strip()
                if jump == END_TARGET:
                    current = None
                elif jump:
                    target = node_map.get(jump)
                    if target is None:
                        status = "failed"
                        error = f"节点「{node['name']}」失败跳转目标「{jump}」不存在，请检查跳转配置"
                        break
                    current = target
                else:
                    status = "failed"
                    error = f"节点「{node['name']}」异常: {e}"
                    break

        duration_ms = int((time.monotonic() - start) * 1000)
        await workflow_service.finish_run(run_id, status, {"output": last_output}, error, duration_ms)
        logger.info(
            f"🔄 工作流 #{workflow_id} 执行完毕: {status}（{duration_ms}ms）"
        )
        return {
            "status": status,
            "output": last_output,
            "ctx": ctx,
            "run_id": run_id,
            "error": error,
            "duration_ms": duration_ms,
            "workflow_name": workflow["name"],
        }

    # ── 节点执行器 ──────────────────────────────────────

    async def _run_node(
        self, node_type: str, params: dict, ctx: dict, agent
    ) -> Tuple[dict, bool]:
        """按节点类型分派执行，返回 (结果dict, 是否成功)"""
        if node_type == "llm":
            return await self._node_llm(params, agent)
        if node_type == "table_query":
            return await self._node_table_query(params, agent)
        if node_type == "table_create":
            return await self._node_table_create(params, agent)
        if node_type == "table_insert":
            return await self._node_table_write("insert", params, agent)
        if node_type == "table_update":
            return await self._node_table_write("update", params, agent)
        if node_type == "table_delete":
            return await self._node_table_write("delete", params, agent)
        if node_type == "http_get":
            return await self._node_http("get", params)
        if node_type == "http_post":
            return await self._node_http("post", params)
        if node_type == "device_command":
            return await self._node_device_command(params, ctx)
        if node_type == "task_create":
            return await self._node_task_create(params)
        if node_type == "condition":
            return await self._node_condition(params, ctx)
        if node_type == "switch":
            return await self._node_switch(params, ctx)
        if node_type == "code":
            return await self._node_code(params, ctx)
        if node_type == "output":
            return {"text": str(params.get("text") or "")}, True
        raise ValueError(f"未知节点类型: {node_type}")

    async def _node_llm(self, params: dict, agent) -> Tuple[dict, bool]:
        # LLM 节点可显式指定使用的智能体（params.agent_id，选填）；
        # 指定后该节点使用所选智能体的模型/系统提示词/知识库（如安全审查 agent）；
        # 默认为空 → 使用当前调用此工作流的智能体（执行身份）
        node_agent_id = params.get("agent_id")
        if node_agent_id:
            try:
                node_agent_id = int(node_agent_id)
            except (TypeError, ValueError):
                node_agent_id = 0
            if node_agent_id > 0:
                use_agent = await agent_service.get_agent(node_agent_id)
                if use_agent is None:
                    raise ValueError(
                        f"LLM 节点指定的智能体不存在: agent_id={node_agent_id}"
                    )
                agent = use_agent
        if agent is None:
            raise ValueError(
                "LLM 节点需要智能体身份：请由智能体在对话中调用该工作流，"
                "或在测试运行/任务触发时指定执行身份，或在该节点指定 LLM"
            )
        prompt = str(params.get("prompt") or params.get("text") or "").strip()
        if not prompt:
            raise ValueError("LLM 节点缺少 prompt")
        # 与设备聊天自动回复路径保持一致：注入智能体系统提示词 + 知识库 RAG，
        # 否则 LLM 节点会丢失智能体人设与绑定的知识库（RAG 被忽略）
        rag_context = ""
        try:
            rag_context = await knowledge_service.build_rag_context(agent.id, prompt)
        except Exception as e:
            logger.warning(f"LLM 节点知识库检索失败 agent=#{agent.id}: {e}")
        system_text = agent.system_prompt or ""
        if rag_context:
            system_text = (
                f"{system_text}\n\n" if system_text else ""
            ) + (
                "【知识库参考资料】\n"
                f"{rag_context}\n\n"
                "请优先参考上述资料回答；若资料与当前问题无关，忽略资料按自身知识回答。"
            )
        # 节点挂载技能：按 skill_ids 注入行为指导（软，不承载安全强制；
        # 如审查节点可挂「安全审查」技能作为行为基线）
        skill_ids = params.get("skill_ids")
        if skill_ids:
            try:
                if isinstance(skill_ids, str):
                    s = skill_ids.strip()
                    skill_ids = json.loads(s) if s.startswith("[") else []
                ids = [int(x) for x in skill_ids] if isinstance(skill_ids, list) else []
                if ids:
                    node_skills = await skill_service.get_skills_by_ids(ids)
                    if node_skills:
                        skill_block = "\n\n".join(
                            f"【技能：{sk['name']}】\n{sk['content']}" for sk in node_skills
                        )
                        system_text = (
                            f"{system_text}\n\n" if system_text else ""
                        ) + skill_block
            except Exception as e:
                logger.warning(f"节点技能注入失败: {e}")
        messages = []
        if system_text:
            messages.append({"role": "system", "content": system_text})
        messages.append({"role": "user", "content": prompt})
        content = await agent_service._chat_completion(
            agent, messages, allow_extra_tools=False
        )
        return {"content": content}, bool(content)

    async def _node_table_query(self, params: dict, agent) -> Tuple[dict, bool]:
        table_key = str(params.get("table") or "").strip()
        if not table_key:
            raise ValueError("查询表格节点缺少 table 参数")
        agent_id = agent.id if agent else None
        if agent_id is None or not await table_service.check_permission(agent_id, table_key, need_write=False):
            raise ValueError(
                f"工作流执行身份对应的智能体#{agent_id} 未获授权读取表格 {table_key}"
                "（表格管理 → 授权给智能体）"
            )
        limit = int(params.get("limit") or 50)
        if limit > 200:
            limit = 200
        rows = await table_service.query_rows(table_key, where=params.get("where"), limit=limit)
        return {"rows": rows, "count": len(rows)}, True

    async def _node_table_create(self, params: dict, agent) -> Tuple[dict, bool]:
        """创建表格节点：创建自定义表格，并自动授权给执行身份的智能体（读写）"""
        from ..models.table import TableCreateRequest

        name = str(params.get("name") or "").strip()
        if not name:
            raise ValueError("创建表格节点缺少 name 参数")
        fields = params.get("fields")
        if isinstance(fields, str):
            try:
                fields = json.loads(fields.strip())
            except Exception:
                raise ValueError(f"创建表格节点的 fields 不是合法 JSON: {str(fields)[:80]}")
        if not isinstance(fields, list) or not fields:
            raise ValueError(
                '创建表格节点的 fields 必须为字段数组，如 [{"name":"name","type":"varchar","label":"姓名"}]'
            )
        req = TableCreateRequest(
            name=name,
            description=str(params.get("description") or "").strip(),
            fields=fields,
        )
        table_id = await table_service.create_table(req)
        agent_id = agent.id if agent else None
        if agent_id:
            await table_service.set_agent_grants(table_id, {agent_id: 1})
        return {"table_id": table_id, "table_key": f"tbl_{table_id}"}, True

    async def _node_table_write(self, action: str, params: dict, agent) -> Tuple[dict, bool]:
        table_key = str(params.get("table") or "").strip()
        if not table_key:
            raise ValueError(f"{action} 节点缺少 table 参数")
        # 安全设计：智能体不授予删除表格能力，工作流 delete 节点一律拒绝（fail-closed）
        if action == "delete":
            raise ValueError(
                "删除表格数据已禁用：按安全设计，智能体不授予删除表格能力"
            )
        agent_id = agent.id if agent else None
        if agent_id is None or not await table_service.check_permission(agent_id, table_key, need_write=True):
            raise ValueError(
                f"工作流执行身份对应的智能体#{agent_id} 对表格 {table_key} 无写权限"
                "（表格管理 → 授权改为「读写」）"
            )
        if action == "insert":
            row = params.get("row")
            if isinstance(row, str):
                try:
                    row = json.loads(row.strip())
                except Exception:
                    raise ValueError(
                        f"插入表格节点的 row 不是合法 JSON 对象: {str(row)[:80]}"
                    )
            if not isinstance(row, dict) or not row:
                raise ValueError("插入表格节点的 row 必须为 JSON 对象，如 {\"name\":\"王五\",\"level\":3}")
            new_id = await table_service.insert_row(table_key, row)
            return {"id": new_id}, True
        if action == "update":
            fields = params.get("fields")
            if isinstance(fields, str):
                try:
                    fields = json.loads(fields.strip())
                except Exception:
                    raise ValueError(
                        f"更新表格节点的 fields 不是合法 JSON 对象: {str(fields)[:80]}"
                    )
            if not isinstance(fields, dict):
                raise ValueError("更新表格节点的 fields 必须为 JSON 对象，如 {\"status\":\"已发货\"}")
            ok = await table_service.update_row(
                table_key, int(params.get("id") or 0), fields
            )
            return {"ok": ok}, ok
        # delete
        ok = await table_service.delete_row(table_key, int(params.get("id") or 0))
        return {"ok": ok}, ok

    async def _node_http(self, method: str, params: dict) -> Tuple[dict, bool]:
        import httpx

        url = str(params.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"URL 必须以 http:// 或 https:// 开头（收到：{url}）")
        headers = {"User-Agent": "Mozilla/5.0 (Aivbot Workflow)"}
        extra_headers = params.get("headers")
        if isinstance(extra_headers, dict):
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20), follow_redirects=True
            ) as client:
                if method == "get":
                    resp = await client.get(url, headers=headers)
                else:
                    data = params.get("data")
                    resp = await client.post(
                        url,
                        headers=headers,
                        json=data if isinstance(data, (dict, list)) else {},
                    )
            text = resp.text or ""
            if len(text) > 6000:
                text = text[:6000] + "\n…[内容过长已截断]"
            return {"status_code": resp.status_code, "text": text}, True
        except Exception as e:
            raise ValueError(f"联网请求失败: {e}")

    async def _node_device_command(self, params: dict, ctx: dict) -> Tuple[dict, bool]:
        robot_id = str(ctx.get("input", {}).get("robot_id") or "")
        cmd_type = int(params.get("cmd_type") or 0)
        if not cmd_type:
            raise ValueError("设备指令节点缺少 cmd_type")
        extra = params.get("params") or {}
        if not isinstance(extra, dict):
            extra = {}
        extra = self._normalize_extra(extra)
        # 502 获取好友资料：设备端 getFriendInfo 的 selectList 参数非空（Kotlin 运行时
        # 校验，缺失抛 "message.selectList must not be null"）。未传 selectList 时用
        # friend.name 补齐（与测试页 /test/friend_info 发送的 extra 一致）。
        if cmd_type == 502 and not extra.get("selectList"):
            _friend = extra.get("friend")
            if isinstance(_friend, dict):
                _name = _friend.get("name")
                if _name:
                    extra["selectList"] = [_name]
        expect_type = str(params.get("expect_type") or "")
        wait_sec = float(params.get("wait_sec") or 8)
        if wait_sec > 60:
            wait_sec = 60

        # 模拟模式（测试运行不依赖真实设备）：直接生成虚拟设备返回数据
        if ctx.get("_simulate"):
            result = self._mock_device_result(cmd_type, expect_type, extra)
            # 短暂延迟模拟设备响应时间，让日志展示正常耗时
            await asyncio.sleep(min(0.8, wait_sec))
            return {"result": result, "cmd_type": cmd_type, "simulated": True}, True

        ok = await aivbot_service.send_command(robot_id, cmd_type, extra)
        if not ok:
            return {"error": f"设备 {robot_id} 离线或 WebSocket 未连接，指令未下发"}, False

        # 设备端机制：收到任何操作指令都会先把接收主循环置停，指令执行完
        # 依赖设备端自动补发的 202(LOOP_RECEIVE_NEW_MESSAGE) 恢复接收。
        # 若设备端异常/卡顿导致未恢复，设备会长时间退出自动接收。这里在指令
        # 下发成功后补发一次 202 作保险（仅当服务器认为该设备本就在接收中，
        # 避免覆盖用户手动「停止任务」的意图）。
        if aivbot_service.task_status.get(robot_id) is True:
            try:
                await aivbot_service.send_command(robot_id, 202)
            except Exception as e:
                logger.warning(f"设备 {robot_id} 补发 202 失败: {e}")

        if expect_type in _EXPECT_KEYS:
            key = _EXPECT_KEYS[expect_type]
            match_field, match_value = None, None
            if expect_type == "501":  # 等指定群详情：群详情按群名去重合并，需按群名匹配
                select_list = extra.get("selectList") or []
                if select_list:
                    match_field, match_value = "group_name", str(select_list[0])
            result = await self._wait_device_result(
                robot_id, key, wait_sec, match_field=match_field, match_value=match_value
            )
        elif cmd_type in _CMD_DATA_KEYS:
            # 未填 expect_type 但为获取类指令（501/502/…）：设备主要上报数据，
            # 部分指令（501/503/505/506）只发数据、不发执行回执，等 last_result 会超时。
            # 数据与回执同时等：失败类指令（如 502 未找到好友）只回执，也能及时返回。
            key = _CMD_DATA_KEYS[cmd_type]
            match_field, match_value = None, None
            if cmd_type == 501:
                select_list = extra.get("selectList") or []
                if select_list:
                    match_field, match_value = "group_name", str(select_list[0])
            result = await self._wait_any_result(
                robot_id, [key, "last_result"], wait_sec,
                match_field=match_field, match_value=match_value,
            )
        else:
            # 无 expect_type → 等待设备执行回执（socketType=3）
            result = await self._wait_device_result(robot_id, "last_result", wait_sec)
        return {"result": result, "cmd_type": cmd_type}, True

    async def _node_task_create(self, params: dict) -> Tuple[dict, bool]:
        """创建任务节点：{name, trigger_type, trigger_spec, action_type, action_spec,
        robot_id?, org_id?, enabled?} → 复用 task_service.create_task
        表单模式：未提供 trigger_spec/action_spec 时，从扁平字段组装
        （trigger_cron/trigger_interval/trigger_cond_*；action_workflow_id/action_agent_id/...）"""
        # 延迟导入避免循环依赖（task_service 顶层导入 workflow_engine）
        from ..models.task import TaskCreateRequest
        from .task_service import build_cron_from_form, parse_datetime_local, task_service

        now = int(time.time())

        def _as_json(v):
            if isinstance(v, str):
                try:
                    return json.loads(v.strip())
                except Exception:
                    return {}
            return v or {}

        def _build_trigger_spec() -> dict:
            if str(params.get("trigger_type") or "").strip() == "schedule":
                spec = {}
                # 单次模式：绝对时间（datetime-local / 时间戳）或相对当前时间 ± 分钟
                if str(params.get("trigger_mode") or "repeat") == "once":
                    once_ts = parse_datetime_local(params.get("trigger_once_at"))
                    if once_ts is None:
                        off = params.get("trigger_once_offset_minutes")
                        if off not in (None, ""):
                            once_ts = now + int(off) * 60
                    if once_ts is not None:
                        spec["once_at"] = once_ts
                    return spec
                # 重复模式：先分重复方式（定时计划 cron / 固定间隔），cron 再分定义方式（选择器 / 表达式）
                method = str(params.get("repeat_method") or "cron")
                if method == "interval":
                    iv = params.get("trigger_interval")
                    if iv not in (None, ""):
                        spec["interval_sec"] = int(iv)
                else:
                    cron = None
                    if str(params.get("cron_def") or "selector") == "format":
                        cron = str(params.get("trigger_cron") or "").strip()
                    else:
                        cron = build_cron_from_form(
                            params.get("trigger_time"),
                            str(params.get("trigger_freq") or "daily"),
                            params.get("trigger_weekdays"),
                            params.get("trigger_monthday"),
                        )
                        if cron is None:  # 时间组件未填 → 回退高级 cron / 旧数据
                            cron = str(params.get("trigger_cron") or "").strip()
                    if cron:
                        spec["cron"] = cron
                # 公共：重复次数 + 可选延迟开始偏移（两种重复方式通用）
                rc = params.get("trigger_repeat_count")
                if rc not in (None, ""):
                    rc = int(rc)
                    if rc > 0:
                        spec["repeat_count"] = rc
                off = params.get("trigger_offset_minutes")
                if off not in (None, ""):
                    off = int(off)
                    if off:
                        spec["offset_sec"] = off * 60
                return spec
            spec = {"type": str(params.get("trigger_cond_type") or "").strip()}
            if spec["type"] == "friend_accepted":
                target = {}
                name = str(params.get("trigger_cond_name") or "").strip()
                phone = str(params.get("trigger_cond_phone") or "").strip()
                if name:
                    target["name"] = name
                if phone:
                    target["phone"] = phone
                if target:
                    spec["target"] = target
            elif spec["type"] == "message_from":
                name = str(params.get("trigger_cond_name") or "").strip()
                if name:
                    spec["target"] = name
            return spec

        def _build_action_spec() -> dict:
            if str(params.get("action_type") or "").strip() == "workflow":
                wid = params.get("action_workflow_id")
                spec = {"workflow_id": int(wid)} if wid not in (None, "") else {}
                # 参数表单：action_wf_param_key[] / action_wf_param_val[] 成对组装；
                # 兼容旧表单的 action_workflow_params JSON 字符串
                p = {}
                keys = params.get("action_wf_param_key") or []
                if keys:
                    vals = params.get("action_wf_param_val") or []
                    for i, k in enumerate(keys):
                        k = str(k or "").strip()
                        if not k:
                            continue
                        v = vals[i] if i < len(vals) else ""
                        if isinstance(v, (list, tuple)):
                            v = v[0] if v else ""
                        try:
                            p[k] = json.loads(str(v))  # 数字/布尔/JSON 保持类型
                        except Exception:
                            p[k] = v
                else:
                    wp = params.get("action_workflow_params")
                    if wp not in (None, ""):
                        try:
                            p = json.loads(str(wp).strip())
                        except Exception:
                            raise ValueError("工作流参数必须是合法 JSON 对象")
                if p:
                    spec["params"] = p
                return spec
            spec = {}
            aid = params.get("action_agent_id")
            if aid not in (None, ""):
                spec["agent_id"] = int(aid)
            instruction = str(params.get("action_instruction") or "").strip()
            if instruction:
                spec["instruction"] = instruction
            room = str(params.get("action_room") or "").strip()
            if room:
                spec["room"] = room
            sr = params.get("action_send_reply")
            if isinstance(sr, str):
                spec["send_reply"] = sr.strip().lower() not in ("0", "false", "no", "")
            elif sr is not None:
                spec["send_reply"] = bool(sr)
            return spec

        legacy = params.get("trigger_spec") is not None or params.get("action_spec") is not None
        trigger_spec = _as_json(params.get("trigger_spec")) if legacy else _build_trigger_spec()
        action_spec = _as_json(params.get("action_spec")) if legacy else _build_action_spec()

        enabled_raw = params.get("enabled", True)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() not in ("0", "false", "no", "")
        else:
            enabled = bool(enabled_raw)
        # 条件任务过期时间（与后台任务表单一致）：trigger_expires_hours 小时后未触发自动停用
        expires_at = int(params.get("expires_at") or 0)
        if str(params.get("trigger_type") or "").strip() == "condition":
            eh = params.get("trigger_expires_hours")
            if eh not in (None, ""):
                try:
                    eh = int(eh)
                    if eh > 0:
                        expires_at = now + eh * 3600
                except (TypeError, ValueError):
                    raise ValueError("过期时间必须是小时数（正整数）")
        req = TaskCreateRequest(
            name=str(params.get("name") or "").strip(),
            org_id=int(params.get("org_id") or 0),
            robot_id=str(params.get("robot_id") or "").strip(),
            trigger_type=str(params.get("trigger_type") or "").strip(),
            trigger_spec=trigger_spec,
            action_type=str(params.get("action_type") or "").strip(),
            action_spec=action_spec,
            enabled=enabled,
            expires_at=expires_at,
        )
        ok, res = await task_service.create_task(req)
        if not ok:
            raise ValueError(f"创建任务失败: {res}")
        return {"task_id": res}, True

    async def _node_condition(self, params: dict, ctx: dict) -> Tuple[dict, bool]:
        field = str(params.get("field") or "").strip()
        if not field:
            raise ValueError("条件节点缺少 field")
        op = str(params.get("op") or "==")
        value = params.get("value")
        actual = self._lookup(ctx, field)
        matched = self._compare(actual, op, value)
        return {"matched": matched, "field": field, "actual": actual, "value": value}, matched

    async def _node_switch(self, params: dict, ctx: dict) -> Tuple[dict, bool]:
        """多分支判断：按 field 的值从上到下匹配 branches，命中分支返回其 next"""
        field = str(params.get("field") or "").strip()
        if not field:
            raise ValueError("多分支判断节点缺少 field")
        actual = self._lookup(ctx, field)
        branches = params.get("branches") or []
        if not isinstance(branches, list):
            branches = []
        for b in branches:
            if not isinstance(b, dict):
                continue
            op = str(b.get("op") or "==")
            val = b.get("value")
            if self._compare(actual, op, val):
                return {
                    "matched": str(b.get("label") or "分支"),
                    "field": field,
                    "actual": actual,
                    "next": str(b.get("next") or ""),
                }, True
        # 全不命中 → 兜底分支（default_next），空则中止
        dft = str(params.get("default_next") or "")
        return {"matched": None, "field": field, "actual": actual, "next": dft}, bool(dft)

    async def _node_code(self, params: dict, ctx: dict) -> Tuple[dict, bool]:
        """代码节点：运行用户 Python 代码，以 output 变量作为节点结果"""
        code = str(params.get("code") or "").strip()
        if not code:
            raise ValueError("代码节点缺少 code")

        # 受限内建白名单：不放 __import__/open/eval 等危险内建
        safe_builtins = {
            "len": len, "str": str, "int": int, "float": float, "bool": bool,
            "list": list, "dict": dict, "set": set, "tuple": tuple,
            "range": range, "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
            "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
            "sorted": sorted, "isinstance": isinstance, "type": type, "print": print,
            "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
            "TypeError": TypeError, "IndexError": IndexError,
            "True": True, "False": False, "None": None,
        }

        def _get(path: str):
            return self._lookup(ctx, str(path))

        env = {
            "__builtins__": safe_builtins,
            "ctx": ctx,
            "data": ctx.get("input") or {},
            "output": {},
            "get": _get,
            "json": json,
            "math": math,
            "re": re,
            "random": random,
            "datetime": datetime,
            "time": time,
        }
        exec(code, env)
        res = env.get("output")
        if not isinstance(res, dict):
            res = {"result": res}
        else:
            r = env.get("result")
            if r is not None and "result" not in res:
                res["result"] = r
        return res, True

    # ── 辅助 ────────────────────────────────────────────

    async def _wait_device_result(
        self, robot_id: str, key: str, wait_sec: float,
        match_field: str = None, match_value: str = None,
    ) -> dict:
        """下发指令后轮询设备上报/回执，返回最新一条结果。

        match_field/match_value：指定后等待“该字段==该值”的条目（用于等指定群详情，
        因为 group_info_detail 按群名去重合并后，列表可能同时含有多个群）。检测改用
        整个列表的 JSON 签名变化（而非只盯末尾对象引用），保证任意位置条目的新增/
        更新都能被识别，避免去重合并后“更新的群不在列表末尾”时漏检超时。
        """
        return await self._wait_any_result(
            robot_id, [key], wait_sec, match_field=match_field, match_value=match_value,
        )

    async def _wait_any_result(
        self, robot_id: str, keys: list, wait_sec: float,
        match_field: str = None, match_value: str = None,
    ) -> dict:
        """等待 keys 中任意一个 key 的结果发生变化，返回第一个变化条目。

        用于获取类指令（501/502/…）：设备可能只上报数据（如 501 无执行回执）、
        可能数据与回执都上报（如 508）、失败时可能只上报失败回执（如 502 未找到好友）。
        - last_result 直接返回最新一条（成功/失败回执）；
        - 数据 key 按 match_field/match_value 匹配（如 501 按群名），未命中继续等。
        """
        received = aivbot_service.get_received_data(robot_id)
        old_sigs = {
            key: json.dumps(received.get(key) or [], ensure_ascii=False, default=str)
            for key in keys
        }

        async def _poll():
            while True:
                received = aivbot_service.get_received_data(robot_id)
                for key in keys:
                    cur = received.get(key) or []
                    if cur and json.dumps(cur, ensure_ascii=False, default=str) != old_sigs[key]:
                        if key == "last_result":
                            return cur[-1]
                        if match_field is not None and match_value is not None:
                            targets = [
                                it for it in cur if str(it.get(match_field)) == str(match_value)
                            ]
                            if targets:
                                return targets[-1]
                            # 列表变了但目标条目尚未出现（如先上报了其他群）→ 继续等
                        else:
                            return cur[-1]
                await asyncio.sleep(0.5)

        try:
            return await asyncio.wait_for(_poll(), timeout=wait_sec)
        except asyncio.TimeoutError:
            return {"error": f"等待设备结果超时（{wait_sec}s，key={'+'.join(keys)}）"}

    def _mock_device_result(self, cmd_type: int, expect_type: str, extra: dict) -> dict:
        """生成虚拟设备返回数据（模拟模式，测试运行不依赖真实设备）。

        数据结构与真实设备上报保持一致（见 _process_item 各 type 分支），
        后续节点可用 {{节点名.字段}} 正常引用；带 simulated=True 标记。
        """
        from datetime import datetime as _dt

        now = _dt.now().strftime("%H:%M:%S")

        def _first(*keys) -> str:
            for k in keys:
                v = extra.get(k)
                if isinstance(v, list) and v:
                    return str(v[0])
                if isinstance(v, str) and v.strip():
                    return v.split(",")[0].strip()
            return ""

        title = _first("titleList", "selectList") or "测试会话"
        group_name = _first("groupName") or "测试群"
        friend_name = _first("friend_name")
        fr = extra.get("friend")
        if isinstance(fr, dict):
            friend_name = str(fr.get("name") or friend_name or "测试好友")

        base = {"time": now, "simulated": True}

        # 空 expect_type 但为获取类指令：与真实模式一致按 cmd_type 返回数据
        # （501/503/505/506 设备只上报数据、不发执行回执，模拟也直接给数据）
        if not expect_type and cmd_type in _CMD_DATA_KEYS:
            expect_type = str(cmd_type)

        if expect_type == "501":  # 群详情
            return {
                **base, "group_name": group_name, "owner": "群主", "member_count": 3,
                "members": ["张伟", "李娜", "王强"],
                "announcement": "欢迎加入测试群（模拟数据）", "remark": "",
            }
        if expect_type == "502":  # 好友资料
            return {
                **base,
                "friend": {
                    "name": friend_name, "phone": "13800000000",
                    "markName": "", "markCorp": "模拟企业", "leavingMsg": "",
                },
            }
        if expect_type == "503":  # 我的信息
            return {
                **base,
                "info": {
                    "name": "测试机器人", "id": "mock_robot_001",
                    "corpName": "模拟企业", "department": "测试部",
                },
            }
        if expect_type == "505":  # 最近聊天
            return {
                **base, "type": "最近聊天", "count": 2,
                "items": [
                    {"title": "文件传输助手", "lastMessage": "你好（模拟数据）"},
                    {"title": group_name, "lastMessage": "测试消息（模拟数据）"},
                ],
            }
        if expect_type == "506":  # 企业列表
            return {**base, "corps": ["模拟企业A", "模拟企业B"]}
        if expect_type == "507":  # 全部好友
            return {
                **base, "type": "全部好友", "count": 3,
                "items": ["好友甲", "好友乙", "好友丙"],
            }
        if expect_type == "508":  # 全部群
            return {
                **base, "type": "全部群", "count": 2,
                "items": ["测试群1", "测试群2"],
            }
        # 无 expect_type → 模拟执行回执（socketType=3）
        return {
            **base, "status": "success",
            "success_list": [f"指令 {cmd_type} 已执行（模拟数据）"],
            "fail_list": [], "reason": "", "raw_msg": "ok",
        }

    @staticmethod
    def _normalize_extra(extra: dict) -> dict:
        """设备指令参数归一化：列表字段逗号拆分；friend_* 包装进 friend 对象（发送前整形）"""
        return build_command_extra(extra)

    @staticmethod
    def _compare(actual, op: str, value) -> bool:
        """条件比较"""
        if op == "==":
            return str(actual) == str(value)
        if op == "!=":
            return str(actual) != str(value)
        if op == "contains":
            return str(value) in str(actual or "")
        if op == "not_contains":
            return str(value) not in str(actual or "")
        if op == "empty":
            return actual is None or str(actual).strip() == ""
        if op == "not_empty":
            return actual is not None and str(actual).strip() != ""
        # 数值比较
        try:
            a, b = float(actual), float(value)
        except (TypeError, ValueError):
            return False
        if op == ">":
            return a > b
        if op == "<":
            return a < b
        if op == ">=":
            return a >= b
        if op == "<=":
            return a <= b
        return False

    def _render(self, obj, ctx):
        """递归渲染 {{变量}} 模板。变量路径如 input.user_text / q.rows.0.order_no"""
        if isinstance(obj, str):
            def _repl(m):
                val = self._lookup(ctx, m.group(1).strip())
                return "" if val is None else str(val)
            return _TMPL_RE.sub(_repl, obj)
        if isinstance(obj, dict):
            return {k: self._render(v, ctx) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._render(v, ctx) for v in obj]
        return obj

    def _lookup(self, ctx: dict, path: str):
        """按路径取值：支持 a.b.c 与列表数字索引 a.0.c"""
        cur: Any = ctx
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(cur, tuple):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
            if cur is None:
                return None
        return cur


# 全局引擎实例
workflow_engine = WorkflowEngine()

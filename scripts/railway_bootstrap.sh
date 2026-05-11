#!/usr/bin/env bash
# 龙虾斗兽场 → Railway 一键初始化
# ============================================================================
# 干的事（用户已经 `railway login` 过的前提下）：
#   1. 在 Railway 创建项目 + 一个名为 `claw-war` 的 service
#   2. 把当前目录链接到这个项目（生成 .railway/）
#   3. 把本地 .env 里的 OPENAI_* 等变量推到 Railway service 的环境变量
#   4. 加一个挂到 /data 的持久化 Volume（用于 lobsters.json / bots.json / feed.json）
#   5. 设 CLAW_WAR_DATA_DIR=/data
#   6. 生成一个 Railway 子域名（默认 claw-war-production.up.railway.app）
#
# 用法：
#   bash scripts/railway_bootstrap.sh
#
# 跑完后你要做的事：
#   去 Railway dashboard → 你的项目 → Settings → Tokens → "Create Project Token"
#   把 token 复制出来交给 agent，由 agent 用 `gh secret set RAILWAY_TOKEN` 写到 repo
#
# 注意：本脚本可以重复跑（幂等）。已存在的项目/service/volume 会跳过。
# ============================================================================
set -euo pipefail

SERVICE_NAME="claw-war"
PROJECT_NAME="claw-war"
VOLUME_MOUNT="/data"

log() { echo -e "\033[36m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[33m[bootstrap]\033[0m $*"; }
err() { echo -e "\033[31m[bootstrap]\033[0m $*" >&2; }

# ---------- 0. 前置检查 ----------
if ! command -v railway >/dev/null 2>&1; then
  err "找不到 railway CLI，先装一下：bash <(curl -fsSL railway.com/install.sh)"
  exit 1
fi

log "当前 Railway 用户："
railway whoami || { err "没登录，请先 railway login"; exit 1; }

# 必须在项目根目录跑（要读 .env / 看到 Dockerfile）
if [[ ! -f Dockerfile || ! -f requirements.txt ]]; then
  err "请在项目根目录跑这个脚本（找不到 Dockerfile / requirements.txt）"
  exit 1
fi

# ---------- 1. 创建或链接项目 ----------
if [[ -d .railway && -f .railway/config.json ]]; then
  log "检测到已有 .railway/，跳过 init"
else
  log "创建 Railway 项目 $PROJECT_NAME ..."
  # -n 指定项目名，避免交互
  railway init -n "$PROJECT_NAME" || {
    warn "init 失败（可能项目已存在），尝试 link 现有项目"
    railway link
  }
fi

log "当前项目状态："
railway status || true

# ---------- 2. 创建 / 选中名为 claw-war 的 service ----------
# `railway add --service NAME` 在新项目里会创建该名字的 empty service
# 已存在时会报错——所以用 || true 兜底，再用 status 验证
log "确保存在 service：$SERVICE_NAME"
railway add --service "$SERVICE_NAME" 2>&1 | sed 's/^/  /' || true

# ---------- 3. 同步 .env 到 Railway 环境变量 ----------
if [[ -f .env ]]; then
  log "把 .env 里的变量推到 Railway service=$SERVICE_NAME ..."
  # 读 .env：跳过注释行 / 空行 / 没有 = 的行
  # 用 awk 切 KEY 和 VALUE（VALUE 里可能含 = 所以只切第一个）
  while IFS= read -r line || [[ -n "$line" ]]; do
    # 去掉行首空白
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue

    key="${line%%=*}"
    val="${line#*=}"
    # 去掉 KEY 两端空白
    key="$(echo -n "$key" | tr -d '[:space:]')"
    # 去掉 VAL 可能的两端引号
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"

    [[ -z "$key" ]] && continue
    log "  set $key"
    railway variables --service "$SERVICE_NAME" --set "$key=$val" >/dev/null
  done < .env
else
  warn "没找到 .env，跳过变量同步（你也可以稍后用 railway variables --set 手动加）"
fi

# 强制覆盖：CLAW_WAR_DATA_DIR 必须指向 volume 挂载点
log "设置 CLAW_WAR_DATA_DIR=$VOLUME_MOUNT"
railway variables --service "$SERVICE_NAME" --set "CLAW_WAR_DATA_DIR=$VOLUME_MOUNT" >/dev/null

# ---------- 4. 加 Volume ----------
log "为 $SERVICE_NAME 添加 Volume 挂到 $VOLUME_MOUNT ..."
# 已存在同挂载点的 volume 会报错，吞掉即可
railway volume add --service "$SERVICE_NAME" --mount-path "$VOLUME_MOUNT" 2>&1 \
  | sed 's/^/  /' \
  || warn "Volume 可能已存在，已跳过"

# ---------- 5. 生成 Railway 域名 ----------
log "生成默认 Railway 子域 ..."
railway domain --service "$SERVICE_NAME" 2>&1 | sed 's/^/  /' || true

# ---------- 6. 提示下一步 ----------
echo
log "==================== 初始化完成 ===================="
log "下一步："
log "  1) 浏览器打开 Railway dashboard：railway open"
log "  2) Project Settings → Tokens → \"Create Project Token\""
log "  3) 复制 token，发给 agent；它会跑："
log "       gh secret set RAILWAY_TOKEN -R wz14/claw_war"
log "  4) 然后 push main，GitHub Actions 自动部署"
log "===================================================="

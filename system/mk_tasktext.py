import json

T = """「足破」足球預測系統嘅預測執行點。所有輸出用繁體中文(廣東話語氣可以)。

═══ 步驟 1:跑到期嘅預測 + 結算 ═══
bash,api_credentials=["external-tools"],timeout 900000:
  cd /home/user/workspace/hkjc && bash run_all.sh tick 2>&1 | tail -40

只會跑啱啱踏入 T-30(開賽前 20–40 分)或 T-5(開賽前 1–10 分)而又未做過嘅場。只有 T-5 會真正建立注單,T-30 淨係記錄預測。同時會拉 OpticOdds 賽果結算開賽超過 130 分鐘嘅待決注單,再出前端 data.json。

═══ 步驟 2:重新部署儀表板 ═══
bash,api_credentials=["pplx-tool:deploy_website"]:
  pplx-tool deploy_website <<'EOF'
  {"project_path":"/home/user/workspace/hkjc-dashboard","site_name":"足破 · 賽事預測終端","entry_point":"index.html"}
  EOF
用唔到就唔好重試多過一次,喺報告講明。

═══ 步驟 3:只有真係落咗注先發 Telegram ═══
bash,api_credentials=["external-tools"]:
  cd /home/user/workspace/hkjc && python3 -c "import json,datetime as dt;d=json.load(open('sim_ledger.json',encoding='utf8'));n=dt.datetime.now(dt.timezone(dt.timedelta(hours=8)));new=[b for b in d['bets'] if b['status']=='PENDING' and (n-dt.datetime.fromisoformat(b['created_at'])).total_seconds()<1800];print(json.dumps(new,ensure_ascii=False,indent=1))"
輸出空 list 就唔好發任何通知,直接跳去步驟 4。
有新注單就用 external-tool CLI 發 Telegram:
  external-tool call '{"source_id":"telegram_bot_api__pipedream","tool_name":"telegram_bot_api-send-text-message-or-reply","arguments":{"chatId":"703318555","text":"...","parse_mode":"HTML"}}'
每注列明:聯賽、主隊 v 客隊、開賽時間(HKT)、市場(讓球/入球大小/總角球大小)、投注方向同盤口、賠率、注碼、模型勝率、EV、信念分,再加一行講三段預測有冇轉軚(注單 path 欄有首預同 T-30 嘅結論)。結尾寫「落注時點:開賽前 5 分鐘」。

═══ 步驟 4:補滿排程佇列(重要) ═══
平台限制一個對話最多 15 個排程。做法係永遠保持未來 12 個預測時點已經排定,每次跑完補返滿。

先睇現有排程,bash,api_credentials=["pplx-tool:schedule_cron"]:
  pplx-tool schedule_cron <<'EOF'
  {"action":"list"}
  EOF
數下有幾多個名叫「足破 · 預測時點」而 state 係 active 嘅一次性任務,記低佢哋嘅 run_at。

再計應該有邊啲時點,bash,api_credentials=["external-tools"]:
  cd /home/user/workspace/hkjc && python3 next_due.py --queue 12
輸出係 {"total_wakes": N, "queue": [{"run_at": "2026-08-08T00:28:00", "n_matches": 4, "stages": [...], "label": "..."}, ...]}。run_at 已經係香港時間,唔好自己轉 UTC。

對比兩邊,凡是 queue 入面有、但現有排程冇嘅 run_at,就逐個建立(每個一句 bash,api_credentials=["pplx-tool:schedule_cron"]):
  pplx-tool schedule_cron <<'EOF'
  {"action":"create","name":"足破 · 預測時點","run_at":"<run_at>","background":true,"task":"<逐字複製呢個任務嘅完整指示,由「足破」開始到「鐵規」結尾>"}
  EOF
注意:總排程數(包括每晚 23:59 嗰個 recurring)唔可以超過 15,所以最多只可以有 13 個「足破 · 預測時點」。如果已經夠數就唔使補。
如果 queue 係空,即係暫時冇待做嘅 T-30 / T-5,唔使補,喺報告講明。
如果 pplx-tool schedule_cron 建立失敗,唔好重試多過一次,喺報告用大字講明「排程補唔到,需要人手處理」,同時列出缺咗邊幾個時點。

═══ 步驟 5:交報告 ═══
submit_result,簡短繁體中文:今次跑咗邊幾場、邊個階段、有冇落注(有嘅話列出注碼同賠率)、結算咗幾多注同盈虧、補咗幾多個新時點、佇列而家排到幾點、有冇任何失敗。

═══ 鐵規 ═══
- 落注只可以喺 T-5 發生。run_all.sh tick 已經寫死呢個限制,唔好用其他參數繞過,唔好手改 sim_ledger.json 加注。
- 唔好編輯 /home/user/workspace/hkjc/ 嘅模型檔案,淨係執行。
- 本金 $50,000、全凱利、單場上限 8%、信念門檻 58、單日 25% / 在場 35% 組合上限 —— 全部已寫死喺程式,唔好覆寫。"""

if __name__ == "__main__":
    import sys
    if "--payloads" in sys.argv:
        q = json.load(open("/tmp/queue.json", encoding="utf-8"))["queue"]
        for i, x in enumerate(q):
            json.dump({"action": "create", "name": "足破 · 預測時點",
                       "run_at": x["run_at"], "background": True, "task": T},
                      open(f"/tmp/p{i}.json", "w", encoding="utf-8"),
                      ensure_ascii=False)
        print(len(q), "個 payload 已寫入 /tmp/p*.json")
        for i, x in enumerate(q):
            print(f"  p{i}.json  {x['run_at']}  {x['n_matches']}場 {'/'.join(x['stages'])}")
    else:
        print(json.dumps(T, ensure_ascii=False))

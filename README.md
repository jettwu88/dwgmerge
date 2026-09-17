# Wellell 圖框合併工具 (dwgmerge)

把舊圖 (B) 合併進公司標準圖框 (A) 的網頁工具。細節、限制、三種合併模式的定義都寫在應用程式內的「說明 / 限制」分頁 (`ABOUT.md`)，這份 README 只講「怎麼把它架起來」。

## 為什麼選這個技術組合

你目前有的資源：GitHub、Streamlit、免費版 Gemini API，沒有 Azure 訂閱。

- **Gemini API 幫不上忙**：DWG/DXF 是二進位/結構化的 CAD 檔案格式，不是文字，Gemini（或任何 LLM）沒辦法用來做「轉檔」或「幾何運算」，所以這個工具完全不需要它，也沒有串接它。如果之後想加「用自然語言下指令調整圖面」這種功能，那時候才用得上 LLM API，但那是另一個功能，跟現在的圖框合併無關。
- **DWG↔DXF 自動轉檔目前做不到**：免費的 ODA File Converter 授權不允許「自動化/伺服器批次」使用；商用轉檔 SDK/雲端 API 要付費，目前沒有預算。所以第一版先做 **DXF only**，使用者自己在 AutoCAD 存成 DXF 上傳、下載回來再自己存成 DWG。之後如果公司願意付費買轉檔服務，只要在 `app.py` 的上傳/輸出那幾行插入轉檔呼叫就好，不用重做整個工具。
- **Streamlit + GitHub**：都是你現有、免費的資源。程式碼放 GitHub，Streamlit Community Cloud 直接讀這個 repo 部署，網址是公開連結（可設定僅限知道連結的人使用），任何人用瀏覽器打開就能用，不需要另外裝軟體、不需要 Azure 訂閱、不需要 IT 額外配合。

## 部署步驟（第一次設定，之後只是改程式碼再 push）

1. **建立 GitHub repo**：把這個資料夾整個 push 上去（新建一個 repo，例如 `dwgmerge`）。
2. **登入 [share.streamlit.io](https://share.streamlit.io)**（用 GitHub 帳號登入即可，免費）。
3. 點「New app」，選你剛剛那個 repo、分支 `main`、主檔案填 `app.py`，按 Deploy。
4. 幾分鐘後會拿到一個網址，像 `https://your-app-name.streamlit.app`，這就是所有人使用的入口 — 電腦、手機瀏覽器都能開。
5. **(選用但建議) 設定基準圖框的持久化儲存**：預設情況下，「更新基準圖框」上傳的新版本只存在這次執行的暫存空間，Streamlit 重新啟動/重新部署就會消失，回到 repo 裡內建的那份。如果想讓「有人上傳新版 A」這件事永久生效、所有人看到同一份最新版本：
   - 到 GitHub -> Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens，建立一個只對這個 repo 有 `Contents: Read and write` 權限的 token。
   - 到 Streamlit 這個 app 的 Settings -> Secrets，貼上 `.streamlit/secrets.toml.example` 裡的內容，把 `token`、`repo` 換成你自己的。
   - 存檔後 app 會自動重啟，之後「更新基準圖框」會直接 commit 回 GitHub repo。

## 本機測試

```bash
pip install -r requirements.txt
streamlit run app.py
```

瀏覽器會自動打開 `http://localhost:8501`。

## 專案結構

```
app.py                  Streamlit 介面（三個分頁：基準圖框管理 / 批次合併 / 說明）
github_store.py         把基準圖框同步到 GitHub repo 的小工具（選用）
dwgmerge_engine/
  core.py               共用的底層邏輯：找標題欄、算乾淨比例、圖層/字型比對…
  merge.py              三種合併模式的主流程
templates/
  Wellell-standard-V7-2004.dxf   內建的預設基準圖框（第一次啟動就有東西可用）
test_engine.py           開發用的驗證腳本（不是 app 的一部分，可以刪除或保留）
```

## 開發中/待實作的更新項目（依使用者要求列出，時機由開發者判斷）

1. **依模型尺寸自動選圖紙**：小於 A4 → A4，直式比例適合 → A4-V，大於 A3 → A3，同時保留手動選擇的下拉選單（不強制自動）。這項也順帶解決「模型尺寸跟選的圖紙尺寸差異過大」衍生出的其他問題（見 `LESSONS_LEARNED.md` 第 3 點）。
2. **半成品自動/手動判定**：依 B 圖裡有沒有 BOM 表（OLE 物件）自動判斷是否為半成品/組裝件，同樣保留手動勾選覆蓋。
3. **輸出格式預設精簡**：預設只輸出 DXF，PNG 預覽圖等其他格式改成用下拉選單/勾選框自己選要不要加，不要預設都打開。
4. **單一檔案輸出不打包 zip**：只上傳/處理一個檔案時，直接提供該檔案本身下載；只有選了多個 B 圖批次處理時才打包成 zip。

## 已知限制（重點摘要，完整版在 app 裡的「說明 / 限制」分頁）

- 只吃 DXF、只吐 DXF（輸出固定另存為 AutoCAD 2000 / `AC1015` 版本）。
- 沒有網頁上的拖曳畫布；重疊的處理方式是「偵測到明確重複就直接用 A 的取代掉 B 的舊版」+「真的卡到標題欄本身就整塊移到圖紙下方待人工歸位」，其餘不主動搬動。
- 模式 1（B 內容 + A 格式）是目前測試最完整的路徑；模式 2、3 邏輯都已實作，但「完全比照另一邊格式」這種規則在各種舊圖千奇百怪的畫法下無法保證 100% 準確，建議合併後開啟確認一次。
- 標題欄新舊值的比對，假設 A、B 屬於同一個模板家族（tag 名稱、欄位相對位置一致）；換成結構完全不同的新模板時，比對可能失準，需要人工核對。

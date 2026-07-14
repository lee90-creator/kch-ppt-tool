KCH-PPT-Tool 0.0.5
초보자용 실행 안내 / Beginner's Guide
============================================================

[한국어 안내]

1. 이 프로그램은 무엇인가요?
------------------------------------------------------------
KCH-PPT-Tool은 PDF, PowerPoint, Word, 텍스트, 이미지 자료를 올리면
AI CLI를 사용해 발표자료(PPTX)를 만드는 Windows용 로컬 웹도구입니다.

프로그램 화면은 내 PC의 브라우저에서 열립니다. 인터넷에 공개되는
웹사이트가 아니며 기본 주소는 http://127.0.0.1:8765 입니다.


2. 실행 전에 확인하세요
------------------------------------------------------------
- Windows 10/11 64비트 PC를 사용하세요.
- 받은 ZIP 파일을 먼저 완전히 압축 해제하세요.
- ZIP 파일 안에서 START.bat을 직접 실행하지 마세요.
- 이전 버전(0.0.3, 0.0.4 등)의 검은 콘솔 창이 열려 있다면 닫으세요.
- 사용할 AI CLI(Codex, Claude 또는 Gemini)가 설치되고 로그인되어 있어야 합니다.
- Python과 pip는 별도로 설치할 필요가 없습니다.


3. 가장 쉬운 실행 방법
------------------------------------------------------------
1) KCH-PPT-Tool-0.0.5.zip을 원하는 폴더에 압축 해제합니다.
2) 압축을 푼 KCH-PPT-Tool 폴더를 엽니다.
3) START.bat을 더블클릭합니다.
4) 검은 콘솔 창을 닫지 말고 기다립니다.
5) 브라우저가 자동으로 열리면 보안 안내를 읽고 동의합니다.

브라우저가 자동으로 열리지 않으면 Chrome 또는 Edge 주소창에
http://127.0.0.1:8765 를 직접 입력하세요.


4. PPT 만드는 방법
------------------------------------------------------------
1) "생성" 화면에서 참고 자료를 선택합니다.
2) 원하는 결과와 강조할 내용을 "요청사항"에 적습니다.
3) 사용 가능한 AI CLI를 선택합니다.
4) 페이지 수, 청중/용도, 이미지 소스, 회사 스타일을 설정합니다.
5) "작업 생성"을 누릅니다.
6) 완료될 때까지 검은 콘솔 창과 브라우저를 닫지 마세요.
7) 상태가 "완료"로 바뀌면 "다운로드"를 누릅니다.

AI 작업은 자료 양과 서비스 상태에 따라 오래 걸릴 수 있습니다.
로그가 잠시 멈춰도 상세 진행과 경과 시간이 계속 갱신되면 정상입니다.


5. 종료 방법
------------------------------------------------------------
가장 안전한 방법은 웹도구의 "설정" 화면에서 "서버 종료"를 누르는 것입니다.
또는 작업이 없는 상태에서 검은 콘솔 창을 선택하고 Ctrl+C를 누르세요.
작업 중에는 서버를 종료하지 마세요.


6. 문제가 생겼을 때
------------------------------------------------------------
A. 웹사이트가 열리지 않음
- 검은 콘솔 창이 열려 있는지 확인하세요.
- http://127.0.0.1:8765 를 직접 열어 보세요.
- 기존 버전 콘솔을 모두 닫은 뒤 START.bat을 다시 실행하세요.

B. "다른 KCH-PPT-Tool 서버가 이미 실행 중" 메시지
- 기존 웹도구의 "설정 > 서버 종료"를 누르거나 기존 검은 콘솔 창을 닫으세요.
- 그 다음 0.0.5의 START.bat을 다시 실행하세요.

C. AI CLI가 "미설치"로 표시됨
- Codex, Claude 또는 Gemini CLI를 설치하고 먼저 로그인하세요.
- 새 명령 프롬프트에서 codex --version 같은 버전 명령이 동작하는지 확인하세요.
- 설치 후 START.bat을 다시 실행하세요.

D. "Selected model is at capacity" 오류
- AI 서비스가 일시적으로 혼잡한 상태입니다.
- 잠시 기다린 뒤 "같은 설정으로 재시도"를 누르세요.
- 이 오류는 ZIP 파일이나 PC 고장이 아닙니다.

E. 작업 실패
- 작업 화면의 "원본 로그 열기"를 누릅니다.
- 작업 ID, 원본 로그, 검은 콘솔 화면 캡처를 담당자에게 전달하세요.


7. AI 이미지 사용
------------------------------------------------------------
AI 이미지 생성을 선택했다면 "설정"에서 지원되는 이미지 API와 키가
필요할 수 있습니다. API 키는 이 PC의 로컬 data 폴더에 저장됩니다.
API 키를 메신저나 이메일로 공유하지 마세요.


8. 자료와 보안
------------------------------------------------------------
- 웹 서버는 내 PC의 127.0.0.1 주소에서만 실행됩니다.
- 다만 선택한 AI CLI/API가 입력 자료를 외부 AI 서비스로 전송할 수 있습니다.
- 회사 보안 정책상 허용된 자료만 사용하세요.
- 작업 정보는 KCH-PPT-Tool의 data 및 workspace 폴더에 저장됩니다.
- 결과를 내려받기 전에는 data/workspace 폴더를 삭제하지 마세요.


9. 직원에게 전달할 파일
------------------------------------------------------------
직원에게는 KCH-PPT-Tool-0.0.5.zip 파일 하나만 전달하면 됩니다.
받은 직원은 압축 해제 후 KCH-PPT-Tool\START.bat을 실행하면 됩니다.


============================================================
[English Guide]

1. What is this tool?
------------------------------------------------------------
KCH-PPT-Tool is a local Windows web tool that uses an AI CLI to create
PowerPoint presentations (PPTX) from PDFs, PowerPoint files, Word files,
text documents, and images.

The interface opens in a browser on your own PC. It is not a public website.
The default local address is http://127.0.0.1:8765.


2. Before you start
------------------------------------------------------------
- Use a 64-bit Windows 10 or Windows 11 PC.
- Fully extract the ZIP file before running the tool.
- Do not run START.bat from inside the ZIP file.
- Close console windows from older versions such as 0.0.3 or 0.0.4.
- Install and sign in to the AI CLI you will use: Codex, Claude, or Gemini.
- You do not need to install Python or pip separately.


3. The easiest way to start
------------------------------------------------------------
1) Extract KCH-PPT-Tool-0.0.5.zip to any folder.
2) Open the extracted KCH-PPT-Tool folder.
3) Double-click START.bat.
4) Keep the black console window open and wait.
5) When the browser opens, read and accept the security notice.

If the browser does not open automatically, open Chrome or Edge and visit:
http://127.0.0.1:8765


4. How to create a presentation
------------------------------------------------------------
1) On the "Create" page, select your source files.
2) Describe the required result and key points in the request box.
3) Select an available AI CLI.
4) Choose the page count, audience, image source, and company style options.
5) Click "Create Job".
6) Keep both the browser and the black console window open until completion.
7) When the status changes to "Done", click "Download".

AI generation may take time depending on the source size and service status.
A short pause in the console is normal when progress and elapsed time continue
to update on the job page.


5. How to stop the tool
------------------------------------------------------------
The safest method is "Settings > Stop Server" in the web tool.
You can also press Ctrl+C in the black console when no job is running.
Do not stop the server while a job is running.


6. Troubleshooting
------------------------------------------------------------
A. The website does not open
- Make sure the black console window is still open.
- Open http://127.0.0.1:8765 manually.
- Close all older-version consoles and run START.bat again.

B. "Another KCH-PPT-Tool server is already running"
- Use "Settings > Stop Server" in the existing tool, or close its console.
- Then run START.bat from version 0.0.5 again.

C. The AI CLI is shown as unavailable
- Install and sign in to Codex, Claude, or Gemini CLI first.
- In a new Command Prompt, check that a command such as codex --version works.
- Restart START.bat after installation.

D. "Selected model is at capacity"
- The AI service is temporarily busy.
- Wait a little, then click "Retry with the same settings".
- This does not mean the ZIP file or your PC is broken.

E. A job fails
- Click "Open Original Log" on the job page.
- Send the job ID, original log, and a console screenshot to the support contact.


7. AI images
------------------------------------------------------------
If you select AI image generation, a supported image API and key may be
required in "Settings". The key is stored locally in this PC's data folder.
Never share an API key through email or a messenger service.


8. Data and security
------------------------------------------------------------
- The web server listens only on the local 127.0.0.1 address.
- The selected AI CLI/API may still send source content to an external AI service.
- Use only documents allowed by your company's security policy.
- Job information is stored in the data and workspace folders.
- Do not delete data/workspace before downloading your result.


9. File to share with employees
------------------------------------------------------------
Share only KCH-PPT-Tool-0.0.5.zip.
The recipient extracts it and runs KCH-PPT-Tool\START.bat.

End of guide.

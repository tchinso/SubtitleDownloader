const HOST = "http://127.0.0.1:48741";
const input = document.getElementById("token");
const status = document.getElementById("status");

chrome.storage.local.get("edgeToken", ({ edgeToken }) => {
  if (edgeToken) input.value = edgeToken;
});

async function request(path, body) {
  const token = input.value.trim();
  if (!token) throw new Error("먼저 앱의 엣지 연동 코드를 붙여넣어 줘");
  await chrome.storage.local.set({ edgeToken: token });
  const response = await fetch(HOST + path, {
    method: body ? "POST" : "GET",
    headers: { "Authorization": `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(5000)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `연결 오류 ${response.status}`);
  return result;
}

async function handle(fn) {
  status.textContent = "연결 중…";
  try {
    await fn();
    status.textContent = "완료. 앱 화면에서 제목이나 링크를 확인해 줘.";
  } catch (error) {
    status.textContent = `연결 실패: ${error.message}. 앱 실행 상태와 연동 코드를 확인해 줘.`;
  }
}

document.getElementById("test").addEventListener("click", () => handle(() => request("/v1/status")));
document.getElementById("send").addEventListener("click", () => handle(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.url !== "string") throw new Error("현재 탭 주소를 읽을 수 없음");
  await request("/v1/send", { title: tab.title || "", url: tab.url });
}));

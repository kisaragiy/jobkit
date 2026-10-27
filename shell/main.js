/**
 * jobkit 桌面壳子 —— Electron 主进程。
 *
 * 只承载同源工作区，业务执行交给 Python 唯一 Kernel。
 * 平台适配尚未注册，不挂载旧 BOSS webview 或共享登录态。
 *
 * 用法：
 *   npm start                # 正常启动
 */
const { app, BrowserWindow, Menu, shell, ipcMain, dialog } = require('electron');
const path = require('path');

const JOBKIT_URL = process.env.JOBKIT_URL || 'http://127.0.0.1:18770';
const SERVICE = new URL(JOBKIT_URL);
if (SERVICE.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(SERVICE.hostname)
    || SERVICE.username || SERVICE.password || SERVICE.pathname !== '/' || SERVICE.search || SERVICE.hash) {
	throw new Error('JOBKIT_URL must be a loopback HTTP origin');
}
const DESKTOP_URL = SERVICE.origin + '/desktop';
const BOSS_URL = 'https://www.zhipin.com/';

let win = null;

function createWindow() {
	win = new BrowserWindow({
		width: 1460,
		height: 950,
		minWidth: 1000,
		minHeight: 640,
		title: 'jobkit 沟通工作台',
		backgroundColor: '#0f172a',
		show: false,
		webPreferences: {
			webviewTag: false,       // 渠道适配尚未注册，不内嵌平台自动化
			contextIsolation: true,
			nodeIntegration: false,
			preload: path.join(__dirname, 'preload.js'),
		},
	});

	win.once('ready-to-show', () => win.show());
	// 同源载入：工具 iframe 与 API 共享 HttpOnly 会话，不再依赖通配 CORS。
	let retryTimer, attempts = 0, startupFailed = false;
	const failStartup = () => {
		if (startupFailed || !win || win.isDestroyed()) return;
		startupFailed = true;
		clearTimeout(retryTimer);
		dialog.showErrorBox('jobkit 界面暂时不可用',
			'桌面界面未能正常运行。请确认本机服务已启动，再从正常应用入口重新打开。\n也可先用浏览器打开：' + SERVICE.origin + '/workspace');
		app.quit();
	};
	const loadDesktop = () => {
		if (startupFailed || !win || win.isDestroyed()) return;
		attempts++;
		win.loadURL(DESKTOP_URL).catch(error => {
			console.error('[desktop-load]', error.code || error.name || 'load-failed');
			if (startupFailed || !win || win.isDestroyed()) return;
			if (attempts < 3) retryTimer = setTimeout(loadDesktop, 3000);
			else failStartup();
		});
	};
	win.webContents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
		if (isMainFrame) console.error('[desktop-load-failed]', code);
	});
	win.webContents.on('render-process-gone', (_event, details) => {
		if (details.reason === 'clean-exit') return;
		console.error('[desktop-renderer-gone]', details.reason, details.exitCode);
		failStartup();
	});
	win.webContents.on('will-navigate', (event, url) => {
		if (url !== DESKTOP_URL) event.preventDefault();
	});
	loadDesktop();

	// 外链一律走系统浏览器，不在壳子里开新窗口
	win.webContents.setWindowOpenHandler(({ url }) => {
		if (/^https?:\/\//i.test(url)) shell.openExternal(url);
		return { action: 'deny' };
	});

	win.on('closed', () => { clearTimeout(retryTimer); win = null; });
}

// 旧登录态桥不会把共享 Cookie 池导入任何新角色或账号。
ipcMain.handle('boss:import-login', async (event) => {
  if (!win || event.sender !== win.webContents || event.senderFrame?.url !== DESKTOP_URL) {
    return { ok: false, error: '不受信任的应用窗口' };
  }
  return { ok: false, code: 'CHANNEL_NOT_REGISTERED', error: '平台账号接入尚未注册，当前仅提供本地草稿。' };
});

function buildMenu() {
	// GetJob 也有 File/Edit/View/Window/Help 这套菜单
	const template = [
		{
			label: 'File',
			submenu: [
				{ label: '重新加载界面', accelerator: 'CmdOrCtrl+R', click: () => win?.reload() },
				{ type: 'separator' },
				{ role: 'quit', label: '退出' },
			],
		},
		{
			label: 'Edit',
			submenu: [
				{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' },
				{ type: 'separator' },
				{ role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' },
				{ role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' },
			],
		},
		{
			label: 'View',
			submenu: [
				{ role: 'reload', label: '重载' },
				{ role: 'toggleDevTools', label: '开发者工具' },
				{ type: 'separator' },
				{ role: 'resetZoom', label: '实际大小' },
				{ role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' },
				{ type: 'separator' },
				{ role: 'togglefullscreen', label: '全屏' },
			],
		},
		{
			label: 'Window',
			submenu: [{ role: 'minimize', label: '最小化' }, { role: 'close', label: '关闭' }],
		},
		{
			label: 'Help',
			submenu: [
				{
					label: 'BOSS 直聘（浏览器打开）',
					click: () => shell.openExternal(BOSS_URL),
				},
				{
					label: '关于 jobkit',
					click: () => {
						const { dialog } = require('electron');
						dialog.showMessageBox(win, {
							type: 'info',
							title: '关于 jobkit',
							message: 'jobkit 沟通工作台',
							detail: `招聘者与求职者沟通工作台\n\n工具界面：${JOBKIT_URL}\n平台账号接入尚未注册，仅提供本地草稿`,
						});
					},
				},
			],
		},
	];
	Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

/* ── 单实例锁 ─────────────────────────────────────────────────
   不加锁时反复启动会积一堆 Electron 进程（实测堆到 12 个），
   每个都持有自己的 webview / 登录分区，互相抢焦点，用户看到多个窗口。
   requestSingleInstanceLock 保证全局只有一个实例；第二次启动会直接退出，
   并把已有的那个窗口顶到前面。 */
if (!app.requestSingleInstanceLock()) {
	app.quit();
} else {
	app.on('second-instance', () => {
		if (win && !win.isDestroyed()) {
			if (win.isMinimized()) win.restore();
			win.show();
			win.focus();
		}
	});

	app.whenReady().then(() => {
		buildMenu();
		createWindow();
		app.on('activate', () => {
			if (BrowserWindow.getAllWindows().length === 0) createWindow();
		});
	});
}

app.on('window-all-closed', () => {
	if (process.platform !== 'darwin') app.quit();
});

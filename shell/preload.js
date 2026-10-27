/**
 * 渲染进程与主进程之间的最小桥。
 *
 * 旧登录态桥仅保留明确的停用提示；不读取或转移任何平台凭据。
 */
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jobkitShell', {
	version: process.versions.electron,
	node: process.versions.node,
	bossUrl: 'https://www.zhipin.com/',

	/** 兼容旧调用：返回 CHANNEL_NOT_REGISTERED，不导入登录态。 */
	importBossLogin: () => ipcRenderer.invoke('boss:import-login'),
});

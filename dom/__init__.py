"""jobkit · BOSS DOM 采集通道（选择器层迁移自 GetJob 2.3.1）。

模块边界：
  boss_selectors.py  选择器 / 文案 / 城市码常量（纯数据）
  boss_extract.js    注入页面执行的两个入口（返回 JSON 字符串）
  dom_channel.py     对外接口 + CDP 执行 + 字段归一（本模块）
"""

"""
@Author: xycdaimi
@Email: xycdaimi@gmail.com
@Date: 2026-04-27
@Description: 提供商插件导出入口
"""
from infrastructure.plugins.providers.anthropic import AnthropicPlugin
from infrastructure.plugins.providers.antigravity_openai import AntigravityOpenAiPlugin
from infrastructure.plugins.providers.antigravity_oauth import AntigravityOauthPlugin
from infrastructure.plugins.providers.gemini import GeminiPlugin
from infrastructure.plugins.providers.gemini_custom import GeminiCustomPlugin
from infrastructure.plugins.providers.gemini_oauth import GeminiOauthPlugin
from infrastructure.plugins.providers.gemini_openai import GeminiOpenAiPlugin
from infrastructure.plugins.providers.gemini_web_proxy import GeminiWebProxyPlugin
from infrastructure.plugins.providers.openai import OpenAIPlugin
from infrastructure.plugins.providers.openrouter import OpenRouterPlugin
from infrastructure.plugins.providers.codex_openai import CodexOpenAiPlugin
from infrastructure.plugins.providers.codex_oauth import CodexOauthPlugin
from infrastructure.plugins.providers.qwen_image_edit import QwenImageEditPlugin

__all__ = [
    "AnthropicPlugin",
    "AntigravityOpenAiPlugin",
    "AntigravityOauthPlugin",
    "GeminiPlugin",
    "GeminiCustomPlugin",
    "GeminiOauthPlugin",
    "GeminiOpenAiPlugin",
    "GeminiWebProxyPlugin",
    "OpenAIPlugin",
    "OpenRouterPlugin",
    "CodexOpenAiPlugin",
    "CodexOauthPlugin",
    "QwenImageEditPlugin",
]

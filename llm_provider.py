"""
LLM Provider Abstraction - Supports multiple LLM backends
"""

from abc import ABC, abstractmethod
from typing import Optional, List
import os


class LLMProvider(ABC):
    """Abstract base class for LLM providers"""
    
    @abstractmethod
    def chat(self, messages: List[dict], system_prompt: str) -> str:
        """
        Send a chat request and return the response.
        
        Args:
            messages: List of message dicts with 'role' and 'content'
            system_prompt: System instructions for the LLM
        
        Returns:
            String response from the LLM
        """
        pass


class OpenAIProvider(LLMProvider):
    """OpenAI API provider."""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        
        from openai import OpenAI
        self.client = OpenAI(api_key=self.api_key)
    
    def chat(self, messages: List[dict], system_prompt: str, max_completion_tokens: int = 1024) -> str:
        """Send chat request to OpenAI"""
        response = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=max_completion_tokens,
            messages=[{"role": "system", "content": system_prompt}] + messages,
        )
        return response.choices[0].message.content or ""


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider (for future use)"""
    
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
        
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        
        import anthropic
        self.client = anthropic.Anthropic(api_key=self.api_key)
    
    def chat(self, messages: List[dict], system_prompt: str, max_completion_tokens: int = 1024) -> str:
        """Send chat request to Anthropic"""
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_completion_tokens,
            system=system_prompt,
            messages=messages
        )
        return response.content[0].text


def get_llm_provider(model: Optional[str] = None) -> LLMProvider:
    """
    Factory function to get the configured LLM provider.
    
    Reads LLM_PROVIDER from environment (default: "openai")
    """
    provider_name = os.getenv("LLM_PROVIDER", "openai").lower()
    
    if provider_name == "openai":
        return OpenAIProvider(model=model)
    elif provider_name == "anthropic":
        return AnthropicProvider(model=model)
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")

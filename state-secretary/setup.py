from setuptools import setup, find_packages

setup(
    name="secretary",
    version="0.1.0",
    description="状态管理秘书模块 - 层级状态管理 + 上下文折叠编号检索 + 缓存命中保障",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=[],
)

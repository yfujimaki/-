@echo off
REM 日経記事かんたん解説アプリの起動スクリプト（Windows用）
REM このファイルをデスクトップにショートカットとして置いて使います。

cd /d "%~dp0"

if not exist venv (
    echo 初回起動: 仮想環境を作成しています...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo 依存パッケージを確認しています...
pip install -r requirements.txt -q

streamlit run app.py

pause

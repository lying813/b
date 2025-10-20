import os
import re
import threading
import time
import requests
import json
import subprocess
from datetime import datetime
from flask import Flask, request, render_template, send_file, redirect, url_for
import yt_dlp

# 初始化Flask应用
app = Flask(__name__)

# 应用核心配置 - 集中管理可配置项，便于后续调整
app.config['DOWNLOAD_FOLDER'] = 'downloads'  # 最终视频/缩略图存储目录
app.config['TEMP_FOLDER'] = 'temp'          # 临时音视频流存储目录
app.config['MAX_FILE_AGE'] = 3600           # 文件自动清理阈值（秒），1小时
app.config['DEBUG'] = True                  # 调试模式，生产环境需关闭
app.config['THUMBNAIL_TIMEOUT'] = 10        # 缩略图下载超时时间（秒）
app.config['AUDIO_PREFERRED_ASR'] = 48000   # 音频优先选择的采样率（Hz）
app.config['AUDIO_BITRATE'] = '192k'        # 合并后音频比特率

# 确保核心目录存在，不存在则自动创建（避免文件操作报错）
os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)


def clean_old_files() -> None:
    """定时清理过期文件（下载目录+临时目录），每30分钟检查一次"""
    while True:
        current_time = time.time()
        try:
            # 遍历需要清理的两个目录
            for target_folder in [app.config['DOWNLOAD_FOLDER'], app.config['TEMP_FOLDER']]:
                # 跳过不存在的目录（防止配置变更导致异常）
                if not os.path.exists(target_folder):
                    continue
                # 遍历目录下所有文件
                for filename in os.listdir(target_folder):
                    file_path = os.path.join(target_folder, filename)
                    # 仅处理文件（排除子目录）
                    if os.path.isfile(file_path):
                        file_age = current_time - os.path.getctime(file_path)
                        # 超过有效期则删除
                        if file_age > app.config['MAX_FILE_AGE']:
                            os.remove(file_path)
                            print(f"[文件清理] 已删除过期文件：{filename}（目录：{os.path.basename(target_folder)}）")
        except Exception as e:
            print(f"[文件清理错误] 执行清理时发生异常：{str(e)}")
        # 每30分钟（1800秒）执行一次清理
        time.sleep(1800)


def is_valid_bilibili_url(url: str) -> bool:
    """验证URL是否为合法的B站视频链接（支持标准链接和短链接）
    Args:
        url: 待验证的URL字符串
    Returns:
        合法返回True，非法返回False
    """
    bilibili_url_patterns = [
        r'https?://www\.bilibili\.com/video/[a-zA-Z0-9_?=/-]+',  # 标准视频链接（如BV号链接）
        r'https?://b23\.tv/[a-zA-Z0-9]+'                          # 短链接（b23.tv格式）
    ]
    # 检查URL是否完全匹配任一正则模式（避免部分匹配无效链接）
    return any(re.fullmatch(pattern, url) for pattern in bilibili_url_patterns)


def sanitize_filename(title: str) -> str:
    """清理文件名中的非法字符（Windows系统禁止字符），避免文件创建失败
    Args:
        title: 原始文件名（通常为视频标题）
    Returns:
        清理后的安全文件名
    """
    # 移除Windows系统禁止的文件名字符：\ / : * ? " < > |
    illegal_char_pattern = r'[\\/*?:"<>|]'
    return re.sub(illegal_char_pattern, "", title).strip()


def download_thumbnail(thumbnail_url: str, filename_prefix: str) -> str | None:
    """下载B站视频缩略图到本地下载目录
    Args:
        thumbnail_url: 缩略图在线URL
        filename_prefix: 文件名前缀（与对应视频关联）
    Returns:
        成功返回缩略图文件名，失败返回None
    """
    try:
        # 生成缩略图文件名（固定jpg格式）
        thumbnail_filename = f"{filename_prefix}_thumb.jpg"
        thumbnail_path = os.path.join(app.config['DOWNLOAD_FOLDER'], thumbnail_filename)

        # 模拟浏览器请求头，避免被B站反爬拦截
        request_headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
            'Referer': 'https://www.bilibili.com/'  # 伪造来源页，提高请求成功率
        }

        # 发送GET请求获取缩略图（设置超时防止阻塞）
        response = requests.get(
            url=thumbnail_url,
            headers=request_headers,
            timeout=app.config['THUMBNAIL_TIMEOUT']
        )
        response.raise_for_status()  # 状态码非200时抛出HTTPError

        # 二进制写入文件
        with open(thumbnail_path, 'wb') as f:
            f.write(response.content)

        print(f"[缩略图下载] 成功：{thumbnail_filename}")
        return thumbnail_filename

    except Exception as e:
        print(f"[缩略图下载错误] 失败：{str(e)}")
        return None


def merge_audio_video(video_path: str, audio_path: str, output_path: str) -> bool:
    """通过subprocess调用FFmpeg命令行工具合并音视频
    Args:
        video_path: 纯视频流文件路径
        audio_path: 纯音频流文件路径
        output_path: 合并后最终MP4文件路径
    Returns:
        合并成功返回True，失败返回False
    """
    try:
        # 前置检查：确保输入文件存在
        if not os.path.exists(video_path):
            print(f"[音视频合并错误] 视频文件不存在：{os.path.basename(video_path)}")
            return False
        if not os.path.exists(audio_path):
            print(f"[音视频合并错误] 音频文件不存在：{os.path.basename(audio_path)}")
            return False

        # FFmpeg命令行参数：覆盖输出、复制视频流、转AAC音频、设置比特率、仅输出错误日志
        ffmpeg_command = [
            'ffmpeg',
            '-y',                          # 覆盖已存在的输出文件
            '-i', video_path,              # 输入纯视频流
            '-i', audio_path,              # 输入纯音频流
            '-c:v', 'copy',                # 直接复制视频流（不重新编码，节省时间）
            '-c:a', 'aac',                 # 将音频转换为通用AAC格式（提升兼容性）
            '-b:a', app.config['AUDIO_BITRATE'],  # 音频比特率（从配置读取，统一标准）
            '-strict', 'experimental',     # 允许使用实验性AAC编码器（兼容部分环境）
            '-loglevel', 'error',          # 仅输出错误日志，减少冗余信息
            output_path                    # 合并后输出路径
        ]

        # 执行FFmpeg命令，捕获输出和错误
        result = subprocess.run(
            ffmpeg_command,
            capture_output=True,
            text=True
        )

        # 检查命令执行结果（returncode=0为成功）
        if result.returncode != 0:
            print(f"[FFmpeg错误] 命令执行失败：{result.stderr}")
            return False

        print(f"[音视频合并] 成功：{os.path.basename(output_path)}")
        return True

    except Exception as e:
        print(f"[音视频合并错误] 执行异常：{str(e)}")
        return False


# 启动文件自动清理线程（daemon=True：主线程退出时子线程自动终止）
cleaner_thread = threading.Thread(target=clean_old_files, daemon=True)
cleaner_thread.start()
print(f"[服务启动] 文件自动清理线程已启动，每30分钟清理过期文件")


@app.route('/', methods=['GET', 'POST'])
def index():
    """首页视图：处理GET请求（展示页面）和POST请求（音视频下载+合并）"""
    if request.method == 'POST':
        # 获取用户提交的视频链接（去除首尾空格）
        video_url = request.form.get('video_url', '').strip()

        # 1. 验证链接是否为空
        if not video_url:
            return render_template('index.html', error="请输入有效的B站视频链接")

        # 2. 验证链接是否合法
        if not is_valid_bilibili_url(video_url):
            return render_template('index.html', error="无效的B站视频链接（支持标准链接和b23.tv短链接）")

        try:
            # 生成时间戳（用于文件名去重）
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

            # 3. 获取视频元信息（不下载文件）
            ydl_info_config = {'quiet': True}  # 静默模式，不输出冗余日志
            with yt_dlp.YoutubeDL(ydl_info_config) as ydl:
                video_info = ydl.extract_info(video_url, download=False)
                # 提取关键信息，设置默认值避免KeyError
                video_title = sanitize_filename(video_info.get('title', f'video_{timestamp}'))
                all_formats = video_info.get('formats', [])
                thumbnail_url = video_info.get('thumbnail', '')
                video_duration = video_info.get('duration', 0)  # 视频时长（秒）
                video_uploader = video_info.get('uploader', '未知UP主')

            # 4. 生成文件名前缀（视频标题+时间戳，避免重名）
            filename_prefix = f"{video_title}_{timestamp}"

            # 5. 下载缩略图（仅当缩略图URL存在时执行）
            local_thumbnail = None
            if thumbnail_url:
                local_thumbnail = download_thumbnail(thumbnail_url, filename_prefix)

            # 6. 分离纯视频流和纯音频流
            pure_video_formats = [
                fmt for fmt in all_formats
                if fmt.get('vcodec') != 'none' and fmt.get('acodec') == 'none'
            ]
            pure_audio_formats = [
                fmt for fmt in all_formats
                if fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none'
            ]

            # 检查是否获取到有效音视频流
            if not pure_video_formats or not pure_audio_formats:
                return render_template('index.html', error="无法获取有效的音视频流，请尝试其他视频")

            # 7. 选择最佳视频格式：优先1080P，无则选最高分辨率
            selected_video_format = next(
                (fmt for fmt in pure_video_formats if fmt.get('height') == 1080),
                max(pure_video_formats, key=lambda x: x.get('height', 0))
            )

            # 8. 选择最佳音频格式：优先48000Hz采样率，无则选最高采样率
            selected_audio_format = next(
                (fmt for fmt in pure_audio_formats if fmt.get('asr', 0) >= app.config['AUDIO_PREFERRED_ASR']),
                max(pure_audio_formats, key=lambda x: x.get('asr', 0))
            )

            # 9. 定义文件路径：临时音视频流、最终合并文件
            temp_video_path = os.path.join(
                app.config['TEMP_FOLDER'],
                f"{filename_prefix}_video.{selected_video_format['ext']}"
            )
            temp_audio_path = os.path.join(
                app.config['TEMP_FOLDER'],
                f"{filename_prefix}_audio.{selected_audio_format['ext']}"
            )
            final_video_filename = f"{filename_prefix}.mp4"  # 最终文件固定为MP4格式
            final_video_path = os.path.join(app.config['DOWNLOAD_FOLDER'], final_video_filename)

            # 10. 下载纯视频流
            ydl_video_config = {
                'format': f"{selected_video_format['format_id']}",
                'outtmpl': temp_video_path,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_video_config) as ydl:
                ydl.download([video_url])
            print(f"[视频流下载] 成功：{os.path.basename(temp_video_path)}")

            # 11. 下载纯音频流
            ydl_audio_config = {
                'format': f"{selected_audio_format['format_id']}",
                'outtmpl': temp_audio_path,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_audio_config) as ydl:
                ydl.download([video_url])
            print(f"[音频流下载] 成功：{os.path.basename(temp_audio_path)}")

            # 12. 合并音视频流
            if not merge_audio_video(temp_video_path, temp_audio_path, final_video_path):
                raise RuntimeError("音视频合并失败，请重试")

            # 13. 清理临时文件（合并成功后删除，释放空间）
            for temp_file in [temp_video_path, temp_audio_path]:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                        print(f"[临时文件清理] 已删除：{os.path.basename(temp_file)}")
                    except Exception as e:
                        print(f"[临时文件清理警告] 无法删除：{os.path.basename(temp_file)}，错误：{str(e)}")

            # 14. 处理成功，返回结果页面
            return render_template(
                'index.html',
                success=True,
                filename=final_video_filename,
                video_title=video_title,
                thumbnail=local_thumbnail,
                duration=video_duration,
                uploader=video_uploader
            )

        except Exception as e:
            # 捕获业务异常，返回用户友好提示
            error_msg = f"视频处理失败：{str(e)}"
            print(f"[业务错误] {error_msg}")
            return render_template('index.html', error=error_msg)

    # GET请求：返回首页HTML
    return render_template('index.html')


@app.route('/download/<filename>')
def download_file(filename: str):
    """文件下载接口：返回最终视频文件，触发浏览器下载"""
    file_path = os.path.join(app.config['DOWNLOAD_FOLDER'], filename)

    try:
        # 检查文件是否存在且为有效文件
        if os.path.isfile(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            # 文件不存在（已过期或被清理）
            return redirect(url_for('index', error="待下载文件不存在或已过期（有效期1小时）"))

    except Exception as e:
        error_msg = f"文件下载失败：{str(e)}"
        return redirect(url_for('index', error=error_msg))


@app.route('/thumbnail/<filename>')
def serve_thumbnail(filename: str):
    """缩略图预览接口：返回缩略图文件，用于页面预览"""
    thumbnail_path = os.path.join(app.config['DOWNLOAD_FOLDER'], filename)

    try:
        if os.path.isfile(thumbnail_path):
            return send_file(thumbnail_path, mimetype='image/jpeg')
        else:
            # 缩略图不存在，返回204无内容响应（符合HTTP规范）
            return '', 204

    except Exception as e:
        print(f"[缩略图服务错误] 加载失败：{str(e)}")
        return '', 204


if __name__ == '__main__':
    # 启动Flask应用（host=0.0.0.0允许局域网访问，port=5000为默认端口）
    print(f"[服务启动] Flask应用已启动，访问地址：http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=app.config['DEBUG'])

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shot_web.py —— 给 web/ 界面拍一张 README 用的截图（新版取景器 UI）。

拍出来的状态，等价于「第二次打开这个网站、刚拖入一张照片」：
  - 运行时已就绪（替身 worker 回报「已缓存」，不发真实的 21 MB 下载）
  - 输入区已载入一张参考照片（从 examples/example-photo-vs-svg.jpg 左半裁出的
    「走廊人像」原照片，经页面内 canvas 转成 JPEG——不额外依赖 Pillow）
  - 生成历史里带 3 条示例记录，都对应真实作品（走廊人像 / 夜景人像 / 海边人像）：
    参数取自作品自己的 .json，缩略图是作品原图缩到 128px 的小图（内联在脚本里）
  - 舞台显示刚选中的照片，HUD / 引擎徽标 / 缓存文案全部落在就绪态

用法：
  python3 tools/shot_web.py web/index.html examples/screenshot-web-pc-v4.png
  python3 tools/shot_web.py                                # 全部用默认值（1440×1080 @2x → 2880×2160）
  python3 tools/shot_web.py web/index.html /tmp/x.png --no-history

为什么要单独写个脚本：
  README 顶部那张界面截图要能反映真实布局，手动截图容易漏状态（图选没选、
  按钮是不是可用色、历史区空不空）。这个脚本把状态固定下来，重跑一次得到一样的图。

依赖（不在 requirements.txt 里，只有更新截图时才需要）：
  pip install playwright && playwright install chromium

实现要点：
  - 用本地 http server 而不是 file://：页面要 fetch('worker.js') 探测运行时，
    file:// 下这条路走不通，会退化到「演示模式」，拍出来就不是真实状态。
  - worker.js 换成替身：真的跑起来会下载 21 MB Pyodide，截图不需要；
    替身只回报 ready + 已缓存，页面就落到「第二次打开」的真实分支上。
"""

import argparse
import asyncio
import base64
import functools
import http.server
import os
import shutil
import sys
import tempfile
import threading

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("需要 playwright：pip install playwright && playwright install chromium")


# ---------------------------------------------------------------- 数据

# examples/example-photo-vs-svg.jpg 是一张「左：原照片 / 右：SVG 描摹」的对比图，
# 截图里当「待描摹的照片」用的就是左半边那半张——先按亮度找边界，再裁。
# （边界测量：竖缝在 x≈576，四角暗边约 8px，照片区 x∈[12,572] y∈[42,824]。）
REF_CROP = (12, 42, 572, 824)

# 生成历史的三条示例记录，全部对应真实作品（走廊人像 / 夜景人像 / 海边人像）：
# strokes / dims / size 取自那几件作品自己的参数；thumb 是作品原图缩到最长边
# 128px 的 JPEG（q72，与页面里 thumbOf() 同一规则）。内联在这里是为了让脚本
# 自包含——重跑一次就能得到同样的截图，不依赖仓库外的素材。
HISTORY = [
    {"id": "shot-demo-1", "title": "走廊人像", "name": "走廊人像",
     "dims": "1440 × 2012", "strokes": 172471, "size": 45953778,
     "ago": 2 * 3600 * 1000,
     "thumb": ("data:image/jpeg;base64,"
               "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAkGBwgHBgkIBwgKCgkLDRYPDQwMDRsUFRAWIB0iIiAdHx8kKDQsJCYxJx8fLT0t"
               "MTU3Ojo6Iys/RD84QzQ5Ojf/2wBDAQoKCg0MDRoPDxo3JR8lNzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3"
               "Nzc3Nzc3Nzc3Nzc3Nzf/wAARCACAAFwDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
               "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
               "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
               "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
               "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
               "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
               "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwC5f2wubdl43dVPvXNxsUcoRjnoe1dZ2rnt"
               "btWS7SSPAEmc/UVrNHMOilyAacjZdx75/SqcKyLnLD6VKPMEm4KCMdjWTBFkk+bGPr/KpckfzqrubepKHjNS+YCMHvx0qRky"
               "8AetAY/lTDIoXPBpocBTz70DJWcYoGRnJ5PJqNDnk/gKWR9vHVj0FMCQybeOrHoPWmGIPzIct9SMUxF7k7mPepdvqcU0BuDt"
               "wDWN4h4e2+XqzdPpWwtRXAQ3VuJRlGDA+1b1NiVroc5GpkIWNCSfatG100OT5zkMpwVHauhMfBGFEgADHGM/3W/ofqaxbm7h"
               "s5381woKhsHr3A/lXMpXZfJY2ND8P2F3JKLjzSFUEbWxz+Va7eEdJYYAnX3EgP8ASovB0yS+fIAdjIjKcYyDmujleJFLswVR"
               "1LHGK2SVgOVufBFswzb3jKewkjB/lWJqPhLULZC6IJ0XnMJyfy611txrtpJZzyWEyySRdR0xz157e9Xpb+3gtlnmcKp4zmly"
               "pi0PKC7IdrqQ2cDPSnoMAk8k9TXfazpmnanHHPF8s0wJSWNchsf3h37DPWo9M8OadpCC51maKSUH5Y+qL6cHlj+lQ4saRjaB"
               "4Xu9TCzS/wCj2v8Az0Ycv/uj+vSu6tNJsLOBYYbWIqO7qGJPqSasJcRSIHRsgjgika4VTjgfU1aiWrI4BB0qO4CedCZU3IFJ"
               "PGcc9amQc0kjFLiMhWYeXzjtzWlT4TKO5PAy7jAWDsg+Q55ZSM7T+HQ+1cf4mhaXXgqAsPswP1+cnP8AX8a2ZQ66zG1scQuA"
               "GXH5gen9DWd4suoYroTEs5T92WU4wSR29efw5rkWjNbm7o+rNpdjHHFH5kxgQbPXGeB6nnp1rRvNYuLu3822tYWiZgieccEv"
               "jnjtjNcJbamfsyp+6eHt5h3fkTVoXiXEeL+LdCBxIr4ZB9e49j+daRrOO6IcL7CTXzW2ssvlKjyAq4zlc5zjH4Zq1c6lFqNk"
               "Ir65by0Q+WNu3Y3d/wDa9Mdq5G51CG6vrkRiRgj/ALmVhgHB49wKsw6iy3KRXIURqNqsxyCMZGPY1LqNX0E4tG74U8RTJEsc"
               "kygQlkUSNgAtg7vpgf5zU2va6bu5xIbhdgxHLIyqT7kd+a4s6jb3Hm/Z1aIxgkn154Huae7wmLdeXL3CCDzoQeMnPT8xSc9L"
               "FKLPUPDviGGz8OWzXczedKN/zjO1T396x9akvpL9pdMvZZ7aQB1c579a5rSp7qfbLvKRtg4jPGR04q6b5InZMMSDz8xPNJ1d"
               "LCasdkopG3C5Xau793647mnoKG3fagExnyxnP1NdVX4SY7kEwDXkb7SDu5HfrWXrVlHc3i+au5TMWYEdcDPP5V0AgLyK8p2l"
               "T/D35qtewAXcZIJ+8xz9DXL1NbHCanqFxaaldWYhjks0fGxYRjHp7/WsK7nlNres8cihidsYYlE9SPwrvriysp9YdGti3mSD"
               "MizEA574q0+mx2Tq1rCZEUncJJentjHPFNRW41uebwaZaRGyOm6glzdzr/qQDwR3HHTqPbFbF1pP2m3FwyyN9nUhiAMnJzjr"
               "XfCxMNqxtoVbIxGiKFO09hxxWXcTtao8VxYXMXmHOCVP5VTtuyqitscTb/Z4rln+zP5uM7PLBxnjOPXipVitJVKC0CA8FpI8"
               "AfrXUWP2aZrt0gmUoplcuE5HoKgupLOeHy2iuVUkE4jXsc+tS5RIuzJN9AsoWGNRtXG6NNoGPamW89qkf7xm3Md3KEdavStZ"
               "ouLe2kdmfdh1A9ux6Vm38T3Fy8riCMsfu7zWWm5N9dT0pOBk01lLXIx/zzHP4mpVGRimuCs3Bx+7XP6121PhJjuWEIXheW7s"
               "az9YuFtlMzjO1SAB3JyKvRZLMDxxjisDxVK4eCJOSw3HHXj/APXXNFamplx3c0RV0kOVIIBPp2rr7OH7Wiu+VRhvKnhsHkVx"
               "9nEz3UCyqQpkXdk44zXbtuRXKjkHg54wTW8UuoNu+hamCRSiIEHoT/Sq2qRxTTRRMoIBYjP0H+NKLYzzAtKVY4wwHH0o8yRI"
               "o5JYfMifO4gcqfY/TtQ3FqyBqWrZi21rCdSuowgCDaMCm6pbwQ6XdypGAy4CFRyDwOPzrQNpLbXFxeRuksMhBVufk46NWDrN"
               "xfTQpHYr58c4yUjXOfX+VZNqN7oTTtoZOoRLavckMVZQQOfw4rmRZXUxZ1Y9fWugK38s6RtC7zxkERbdx49QP61E02rwuy/2"
               "ckBJyVaMkn34rnS7aC5WeiJSP/r8npsX+tKlNb5psnsq/wBa76vwkR3JVH5k1y2ryLcX8rKFYKdgyOw9/rmulllEMEsx6RoW"
               "/KuMjnbGWtyzdSQuea54m6Q6NpIpUkRYtysCMnIrstIeS40pZJSpdiMlemBmuNMkj/dtnX/gNdroSlNIgBGCVyRWsddCZO2p"
               "es42LgdgAc+lXtRASydEwVIztI4zmqsThOnfirEkoePkZquRIlzcjLm3xMtxa4VOjKex9CKzJtFtL6YT2DfY7gEl4l4V/XHp"
               "nvir93crbzYxvEhVJIwcZDEgfy4rnDBL4cnXbNJPpkzFoZWOWiPcH39u/Wjk5tzOdRQs2QXOrrpmpyIdNkgmii2hWULxnquM"
               "5zWdc6rd30gnZRGSMbA+cfnXcsLLXLRba+VWYj93Kp5+oP8ASubuvCWoQTNHDGZ4/wCF0xyPcHoaidNONkJ1Kil3R0KUjcTN"
               "6bVoQ0knMrf7q/yrSr8Jcdypqx/0HyhIEMjDn26n+lY/lAA7p9x/32H9as63JDLdLC6ysYl6Icdf8iqcUcBHFtKf956wVrGy"
               "Q/yFcAkE/Rm/xrpNElMlqi7gwT5R9K5g7B8og4z0Lnmtzw7IPmQKFw3QfStIPUU1obcrbdo/GnK+VIzzioLtwLsJ38vP6mhT"
               "ucf7wrQysUroZ1Nc95rcfrIaq2M0QtreyuozLb3ccjOO64bgj0IzWhKA1+PaaIj8mrOtIitxYDulnL+ZJq1scs2+b+u6M+WO"
               "fw7ciGZjNYSndFMPT1Hv6iuntdXcQJwsqkZVw3UVmaaYrzRobXVDvhnBCt/EjD+IVh3djq+kXDWkLTNEPmR4lJVge/t9KdlL"
               "fcxVV0EmleL+9eR0a9KRRmY5PGxf5Uq9qp6pcfZLW6lzyIlC/U5A/nWNT4T0I7nOSym5uZ5/KYh5CRyBx0Hf0FKrKrBRAc++"
               "P8ahhZRHGGY7sbcDGBVksBzvIOccuB/Ss7G4vmsQcWy/iR/hWposzCfLIEAI+771jvKu0jzlB9fMzWhoziQkh93KjOTVR3Je"
               "x0l3812jjvGBn8angKkgbvn3A49v84queZB7DFVftyw69bW7BgGjG5u2CT1/KrILUrf6UP8Arqv6ITVKGXa8Xbbp7v8Azq/f"
               "gRzREdTuJPrhDWWThGOMbdLPP1zWkdTjq6P+uxUuXZdO0bYcFnbOPcitx9eGmObWSQEpgjcDwCAawroZh0KM9QpOPxFVPFD/"
               "APE8uR6bR/46Kuye5yOtKjFyXdL8D//Z")},
    {"id": "shot-demo-2", "title": "夜景人像", "name": "夜景人像",
     "dims": "1440 × 1912", "strokes": 13255, "size": 9263700,
     "ago": 26 * 3600 * 1000,
     "thumb": ("data:image/jpeg;base64,"
               "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAkGBwgHBgkIBwgKCgkLDRYPDQwMDRsUFRAWIB0iIiAdHx8kKDQsJCYxJx8fLT0t"
               "MTU3Ojo6Iys/RD84QzQ5Ojf/2wBDAQoKCg0MDRoPDxo3JR8lNzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3"
               "Nzc3Nzc3Nzc3Nzc3Nzf/wAARCACAAGADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
               "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
               "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
               "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
               "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
               "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
               "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDxOiiimIKKKKACiiigAooooAKKKKACiiig"
               "AooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACl+tJS4oASilxRigBKKKKACiiigApaOPUUuPekAmKMU7FdF4T1Sys"
               "JWhvreIrIwInZM7PY+1DZSVznhG5GdjEf7prb/4RTUkfThMqKt8u9Sjbii+rdh7c16Rc2929h9o0iLT7gkZAOcEexFc54u8Q"
               "asYoY00uTTisXlvJww+iEdB19+aSd0U48rXYz9T8Eva2sk9s005H3YwoyPcnv9BXKXEJQkYwV4Ne0eHZbK+020Tz5G4WRRvw"
               "wPdTjsD2rz/4gRQr4in8iNUDAFyvQk+1BU4q10chSYqXy35wp4ppQjrTMhlFLijFAhtApcUuKLjEpRRilAouBp6Lr2oaNKGs"
               "pyEz80Tcqfwr1Dwxrdt4khcTxbSq4kjPTP8AUV48Fr0X4V6XNI9xeuxWDIRVH8bDr+AzRy32KU+Xc3dK0uLTb6c2bPDbSklk"
               "DHb/AJPtWX410Uak4uNO5liQIUxgMPb3rrJ0fzljhTMuCSeyg+tTrAltCfPfPqSetRZ7mrafungl5DLBMyTI6OOqsMEVXyR0"
               "JFeh/FOKEJYuiKJdzKW7kYrzwiqTMpKzsISfWkJPrS4pMUyR1FFFSMBThSCnAUxD0Fen/DfUwumG08tt0chwQMg55rzJBXd/"
               "D2QxiUDqzcflTUraoOVS0Z6LZyxhnJOWLHcSKy/EOoKNTsLFMFpmLH2Uc1etjsgYMMkkmuHSd7v4gHzAVEKMqA+mOv60Od1Y"
               "pU+WV7mR8R7lpdWityeIo8/if/1VyDCuk8dBh4kn3d0Qj8q5xqlbBL4mRGilNJQIXFLijFOXFAhAKeq0oFPUUAORa7fwiggh"
               "jZuC4bk+tcaign09hXoGnxxzW6vESvzHGec1Vr6IadndnWW0gaBMnnHNc7rlqLfX9O1CJcZcxSEehBwfzq7D5sakK6svY5pL"
               "9/OteSNwG7g5wRzUOLWprzRezOD8eNu8Ryn/AKZJ/KubYV0XjEb9dlcdDEh/SsBxin0M5bsgIppFSMKaRQIBSgUoFOC0CAVK"
               "uaYFp60DJ4z6iu28LTK2nGN+ik1xMQycDrXQ6DMY4pEzjrQyobmtd6hN5yW6t+7f7zDr9K1J2VbEgHB2YH5VyUlwftfXsP51"
               "q3V2VsmYkkBegobb3HFJXsYnifEmpCROd8YBHuDisKRGBYEYKnBB6itmUGWYzTcAdB7dhWXcuZJpH6b2zQS97lNqYR6VMwph"
               "FAj/2Q==")},
    {"id": "shot-demo-3", "title": "海边人像", "name": "海边人像",
     "dims": "1440 × 2162", "strokes": 182640, "size": 61579465,
     "ago": 3 * 24 * 3600 * 1000,
     "thumb": ("data:image/jpeg;base64,"
               "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAkGBwgHBgkIBwgKCgkLDRYPDQwMDRsUFRAWIB0iIiAdHx8kKDQsJCYxJx8fLT0t"
               "MTU3Ojo6Iys/RD84QzQ5Ojf/2wBDAQoKCg0MDRoPDxo3JR8lNzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3Nzc3"
               "Nzc3Nzc3Nzc3Nzc3Nzf/wAARCACAAFUDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
               "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
               "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
               "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
               "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
               "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
               "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDQfT5B2BpqaexPzcV182n47VTe02npXvrE"
               "3Pk/qSi9TItrYxyANnb6V1Ol2olVUVS5UbmyOB7VmR22XHHeuj0GHytznPIwfTFcuKqXjc9XBQs7FHWjlAqgYHb0rzjV4Pnl"
               "dYGYqfmfHAr0jWGw8nIA/lXEX+fPBEny5OQO4NPAy5WaY2PNE49xUTLWrf2ojlOxWCfw7uuKoMmK9xNNXR4l7OxVK1Gy1aK0"
               "xloKUisVoqYrRRYvmPo2a1Vu1Z1zZqO1bhWoJosjpXykKjR9DUoqS2MW0jWO4UkDrxmtxIwjsik4Iz+NZUkQDEryR6VatLls"
               "NHsYn+83SqqrmV0RQfJ7rMLX4iC3lk574rkZ49rM0m5SPukdq9MuLeJkyAGYjJrB1bTi6F9mMDuK1w1fldmTiKLkro82vP3k"
               "jMCxBPVutUnirrdSsI0hdnVBIB8u3qfrXPTRYr36VRSWh83VjKnO0mZrRVEyVfZKidOK1EpFErRVgx80UF8x9GUHmiivjj64"
               "rm3AYsnU9cmmPaIxzyF7gGrJFFNNolxT3KrweXGPJZh64HWqlwJGXEpYqRjpzWsRkY5qndQMRkOfpVRlrqKS00OQ1PTVIOGJ"
               "+owa5W8sHiY4GRXoF3FIM5waxLu33E8V6uHrtdTwsZhoyd7WZxbQH0qJ4j6V1TWQJ5FNFnGDn+Qru+so4o4ST6nLC2Dd8fga"
               "K61LW2Ufdxn14oqHivI3WD01Z6dRRRXzZ9OFJilooFYSsXxZr8Hh3SXvZ4mnYsEjgQgNIx7CtS8uo7SEySH6DuTXn/iazN/r"
               "ukSP5dwI55bi4HVEAUBUz65/lUzlZaGlOHM9TZ8Ma4PEttcC50yfTb23YCW2mOSAwyrDgZB5/KrlxpoPQ/pXIeCtQubjx3rZ"
               "ubeaBWtkjjVwcMI2+8CfXcenbFd9I4rSnVklozGtRi3ZowJbBhwB+lVJLAt1jb/gIroZHAHX9apXtwkEYYk5YhVGe9brFNHM"
               "8JF7GC2mSZ4jf8qKdp2tvNeahBcRptt5dkZXOSOetFafWahCwtPud2L1CM/Lj3bFH26P/ZP0kFYbatYSOYl+1Nx1S3fj8xQ2"
               "nWk8QZDKw/2VC5/AgVyWR18zNttQiU4Iwfdh/jUcmoqvUAAnAyR17d6xZba0K4FqryJgDzQBgVDNBBLGrGCM7WDYIDAEdD0o"
               "sg5mXtVf7ThyOFHI9ayPJjkVwGH7wnLciqlxqCQXAeS5gGw8g4yfbrmteykjmgjeMgpKgcY6c1EtDqozVrMrRXwtFjkCFtpd"
               "cJz2FUtV8STMmy1imRs/eULnH4ml1O0/0aSFGMcgdmUgZBJ9vfpWDHp00ERa9uEjdugIOPyzmnBRUbyZlW5nP3RX8V6rDdIZ"
               "LeaWMbsphRu446dKyr3xXqt1KHXTmxkGIg5Vcj9fWrkdzFHKPKHnFON0h+XH0/8Ar1RmEcULEFYtifKIx0x2rmrVop2i7mtK"
               "hJr3tDAl1DXXnlljUq8jEuUwMnOP8aK3reVpoFk3Nkk8YBFFZfWJmn1aJ27+J4kQG5mtI5Bj/VNvI+vHFUrrxlaMcQ3M0z56"
               "Rjb+veuIi0/Ub4kS3QaA5xFAeM+lbGneFSzATRzxrkfI10efwUf1rvV+pwWS2L48VXV1OIbSxPmnqZDnb7mp0tPEt8zfaNWj"
               "toicgQJlvpWxY6RHBGqwpHEB1C7uauSKIwS86qPXb/8AXpXSHqzDtvC9op33txcXcnfzHwv5LitaOLyPLjt87DhV9F/+tVK/"
               "1SC2jIiYyt6scAVJ4evvtVvPJKQTE/UYxjGe1ZSqpu1zpoQcbysPvmSGYquS3GTnq2a5e9Tz7y5wzxx+a235huIyetdSYPPt"
               "ZZpBiVgXU+meB+lcmyCLgqi7D824YIPrn865qz0R1xSuQvBErhVUBlGQe+fpUc9uHtblA2392w57HHWppxuA5VgWHBzgD8Ov"
               "48Uz7NEY/LZB/slQcLXMaGDZXrxx7QGLcFlzytFS6jpjRzfutoiblR5gHPGaK2916mep6hb2hVdoQInoBVtUihXLbQB3JqCW"
               "5MadBu9jWFfTyyk5b/61d1WqoHm0qTma99rKRKVtsM/97HArnbu8uJ2LSSHFQO+PU471n3rtuQM7jdn5EGc47GuKVSU2d8KU"
               "YIf9qia5+zpud25O0ZA9yc8V0um4i0W5YALubBx9AP61zdihSNvLVX3EkMMdfU+v510sPGlQRMQTIXc8dQoJ/niiHxGltEbk"
               "TB7R0/iVT/8AWrl/EMtvPqDeUI38tFDHjGevP04rU8LXX2iyLNJ5ioRHuIxgY/WuevLJbG8mjjDEPIXJLep//XTm7wElaRW3"
               "EnHlsN3Rge1EkpjZFwxL8cDOPr6U+QOpOwg+m7uaYfOGGcZPPCdKwNCC+tI7hlaRY2IGBvOMUVLG8vImRR6HdRTu0KyZ0d9c"
               "hQV3c98VkvNls81BNc72PPNQCf8AeAb1IJx75rScnJ3MoRUVYn87cAVwcn1xj/69QLNJLKEEbAFsDP480EyOjmBkAIyGI+XO"
               "een480zSyzzO88gZ4ztUI3yKCO3v9aVrIq5ftoWghRCNzMcBEGFH0HpU+t6rDp0zQqN9xHEsUcXTAPVs+/SohK/m5QkEcqwG"
               "QKy76xubudrieUNIcDB44pxsr3G/Is6N4iNsjRGBEJOc/wAB9Kz9S1GW4uvO8w7h02tkD8u1PKywxlbiJyvOCDUEEEc4xErR"
               "Ooz8n9aaVgbuNXU5RuMjBicAZ6L749alW9k3BElSQsQSzrjA9OtRT2zDBeE4H8S8Z9yKpuoAJTHLYwevtT5YshyaOgBJAyAD"
               "70Vhx3E0ShfMZfY0UvZMPaI//9k=")},
]

# 替身 worker：真 worker.js 一跑就要下载 21 MB Pyodide。截图只需要界面落到
# 「运行时已缓存 + 就绪」这个真实分支上，所以替身只回两条消息：
#   ready —— 页面收到后：引擎徽标转绿、按钮解锁、状态「就绪」
#   cacheinfo —— 页面问 cachesummary 时答「已缓存」，缓存文案落到「直接读取…」
FAKE_WORKER = """\
/* 截图专用替身（由 tools/shot_web.py 生成，不要提交这个文件）。 */
self.postMessage({ type: 'ready' });
self.addEventListener('message', (e) => {
  const m = e.data || {};
  if (m.type === 'cachesummary') {
    self.postMessage({ type: 'cacheinfo', count: 9, bytes: 22439526, supported: true });
  }
});
"""


# ---------------------------------------------------------------- 浏览器里跑的小脚本

# ① 从参考对比图里裁出照片（舞台上的「已选图片」用）。
#    图片和页面同源（都在临时站点目录里），canvas 不会被跨域污染。
MAKE_PHOTO = """async ([ref, crop]) => {
  const img = new Image();
  img.src = ref;
  await img.decode();
  const [x1, y1, x2, y2] = crop;
  const t = document.createElement('canvas');
  t.width = x2 - x1; t.height = y2 - y1;
  t.getContext('2d').drawImage(img, x1, y1, t.width, t.height, 0, 0, t.width, t.height);
  return t.toDataURL('image/jpeg', 0.92);
}"""

# ② 把三条示例记录写进 IndexedDB（字段与页面 saveHistory() 存的完全一致）。
WRITE_HISTORY = """async (recs) => {
  const open = () => new Promise((res, rej) => {
    const r = indexedDB.open('photo-to-svg', 1);
    r.onupgradeneeded = () => {
      const d = r.result;
      if (!d.objectStoreNames.contains('records')) {
        d.createObjectStore('records', { keyPath: 'id' }).createIndex('at', 'at');
      }
    };
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  const db = await open();
  await new Promise((res, rej) => {
    const t = db.transaction('records', 'readwrite');
    const store = t.objectStore('records');
    for (const rec of recs) {
      store.put(Object.assign({}, rec, {
        at: Date.now() - rec.ago, duration: 90,
        svg: new Blob(['<svg xmlns="http://www.w3.org/2000/svg"/>'], { type: 'image/svg+xml' }),
        html: new Blob(['<!doctype html><title>demo</title>'], { type: 'text/html' }),
      }));
    }
    t.oncomplete = res; t.onerror = () => rej(t.error);
  });
  db.close();
}"""


# ---------------------------------------------------------------- 工具

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """静态服务器，但不往控制台刷访问日志。"""
    def log_message(self, *args):
        pass


def serve(directory):
    handler = functools.partial(QuietHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


async def run(args):
    html = os.path.abspath(args.html)
    if not os.path.isfile(html):
        sys.exit("找不到界面文件：" + html)
    root = os.path.dirname(os.path.dirname(html))          # web/ 的上一级 = 仓库根
    ref = os.path.abspath(args.ref) if args.ref else os.path.join(root, "examples", "example-photo-vs-svg.jpg")
    if not os.path.isfile(ref):
        sys.exit("找不到参考图：" + ref)

    # 拼一个和 GitHub Pages 同构的最小站点：index.html + worker.js（替身）+ 参考图
    site = tempfile.mkdtemp(prefix="shot-web-")
    shutil.copy(html, os.path.join(site, "index.html"))
    shutil.copy(ref, os.path.join(site, "src-ref.jpg"))
    with open(os.path.join(site, "worker.js"), "w", encoding="utf-8") as f:
        f.write(FAKE_WORKER)
    for icon in ("favicon.ico", "apple-touch-icon.png"):
        p = os.path.join(os.path.dirname(html), icon)
        if os.path.isfile(p):
            shutil.copy(p, os.path.join(site, icon))

    photo_path = os.path.join(site, "pick.jpg")            # 待「选入」的照片，落在临时目录
    httpd, port = serve(site)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = await browser.new_context(viewport={"width": args.width, "height": args.height},
                                            device_scale_factor=args.dpr)
            page = await ctx.new_page()
            await page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="load")
            await page.wait_for_timeout(300)

            # ① 参考图 → 照片 JPEG（舞台上的「已选图片」）
            made = await page.evaluate(MAKE_PHOTO, ["src-ref.jpg", list(REF_CROP)])
            with open(photo_path, "wb") as f:
                f.write(base64.b64decode(made.split(",", 1)[1]))

            # ② 写历史记录 → 刷新页面，让页面的 renderHistory() 自然读出这些记录
            if not args.no_history:
                await page.evaluate(WRITE_HISTORY, HISTORY)
                await page.reload(wait_until="load")
                await page.wait_for_selector(".hitem")

            # ③ 等运行时落在「已就绪」：引擎徽标转绿 + 缓存文案出现
            await page.wait_for_function(
                "document.getElementById('engineText').textContent.indexOf('就绪') >= 0",
                timeout=15000)
            await page.wait_for_function(
                "document.getElementById('rtText').textContent.indexOf('缓存') >= 0",
                timeout=15000)

            # ④ 真实地「选」一张图：从文件输入框塞进去，走页面的 show(f) 全流程
            with open(photo_path, "rb") as f:
                data = f.read()
            await page.set_input_files("#pick", {"name": "走廊人像.jpg",
                                                 "mimeType": "image/jpeg", "buffer": data})
            await page.wait_for_function(
                "document.getElementById('srcDims').textContent.indexOf('×') >= 0",
                timeout=10000)
            await page.wait_for_timeout(500)               # 等布局稳定（图片 onload 后才算尺寸）

            await page.screenshot(path=args.out, full_page=True)
            print(f"已保存 {args.out}（视口 {args.width}×{args.height}）")
            await ctx.close()
            await browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(site, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="给 web/ 界面拍 README 截图")
    ap.add_argument("html", nargs="?", default="web/index.html", help="界面文件")
    ap.add_argument("out", nargs="?", default="examples/screenshot-web-pc-v4.png", help="输出 PNG")
    ap.add_argument("--width", type=int, default=1440, help="视口宽（默认 1440，即 PC 两栏布局）")
    ap.add_argument("--height", type=int, default=1080, help="视口高（900 时左栏要滚动，底部「运行时已缓存」那行会被藏住，所以取 1080）")
    ap.add_argument("--dpr", type=float, default=2, help="device_scale_factor：2 = 双倍像素（README 在 Retina 屏/手机上不糊），输出 2880×2160")
    ap.add_argument("--ref", default="", help="参考照片（默认 examples/example-photo-vs-svg.jpg）")
    ap.add_argument("--no-history", action="store_true", help="历史区留空，不塞示例记录")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

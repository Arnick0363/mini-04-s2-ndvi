# -*- coding: utf-8 -*-
"""把面试表述写成 PDF。在项目根目录运行：python scripts/make_interview_pdf.py"""

from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "面试表述-Sentinel-2-NDVI.pdf"
FONT = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"

GREEN = (22, 78, 58)
INK = (32, 36, 38)
MUTED = (90, 96, 98)
RULE = (214, 220, 216)
ROW = (244, 248, 246)


class Brief(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("yahei", size=8)
        self.set_text_color(*MUTED)
        self.cell(0, 6, f"Sentinel-2 NDVI 季节对比  ·  面试表述  ·  {self.page_no()}", align="C")

    def heading(self, text):
        self.ln(2)
        self.set_x(self.l_margin)
        self.set_font("yahei", "B", 13)
        self.set_text_color(*GREEN)
        self.multi_cell(0, 8, text, align="L")
        self.set_draw_color(*GREEN)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def body(self, text):
        self.set_x(self.l_margin)
        self.set_font("yahei", size=10.5)
        self.set_text_color(*INK)
        self.multi_cell(0, 6.4, text, align="L")
        self.ln(1.5)

    def bullet(self, text):
        self.set_font("yahei", size=10.5)
        self.set_text_color(*INK)
        self.set_x(self.l_margin)
        self.cell(6, 6.4, "·")
        self.multi_cell(0, 6.4, text, align="L")
        self.ln(0.6)

    def step(self, number, title, text):
        self.set_x(self.l_margin)
        self.set_font("yahei", "B", 10.5)
        self.set_text_color(*GREEN)
        self.multi_cell(0, 6.2, f"{number}  {title}", align="L")
        self.body(text)

    def table(self, headers, rows, widths):
        self.set_draw_color(*RULE)
        self.set_line_width(0.15)
        self.set_font("yahei", "B", 9)
        self.set_fill_color(*GREEN)
        self.set_text_color(255, 255, 255)
        self.set_x(self.l_margin)
        for header, width in zip(headers, widths):
            self.cell(width, 7.2, header, border=0, align="C", fill=True)
        self.ln()
        self.set_font("yahei", size=9)
        self.set_text_color(*INK)
        for i, row in enumerate(rows):
            self.set_x(self.l_margin)
            self.set_fill_color(*(ROW if i % 2 == 0 else (255, 255, 255)))
            for value, width in zip(row, widths):
                self.cell(width, 6.8, value, border=0, align="C", fill=True)
            self.ln()
        self.ln(2)


def build():
    pdf = Brief(format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_font("yahei", "", FONT)
    pdf.add_font("yahei", "B", FONT_B)
    pdf.add_page()

    pdf.set_fill_color(*GREEN)
    pdf.rect(0, 0, 210, 32, "F")
    pdf.set_xy(16, 8)
    pdf.set_font("yahei", "B", 16)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 8, "面试时可以怎么讲", align="L")
    pdf.set_xy(16, 17)
    pdf.set_font("yahei", size=10)
    pdf.cell(0, 6, "澳门科技大学  ·  空间大数据分析学硕士", align="L")
    pdf.set_y(38)

    pdf.set_font("yahei", "B", 12)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 7, "Sentinel-2 NDVI 计算与季节对比（澳门近岸，10 m）", align="L")
    pdf.ln(1)
    pdf.set_font("yahei", size=9.5)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(0, 5.4, "数字来自本项目 outputs/stats.json。格网 10 m，投影 EPSG:32649。两期为 2026-01-08 与 2026-08-06。", align="L")
    pdf.ln(2)

    pdf.heading("一、三十秒版本")
    pdf.body(
        "用两景 Sentinel-2 L2A，在澳门近岸做 10 米 NDVI，再比较 2026 年 1 月 8 日和 8 月 6 日。"
        "这是定量遥感里的波段运算和季节对比，还没有做土地覆盖分类。"
    )

    pdf.heading("二、处理顺序")
    pdf.step(
        "1",
        "定研究区",
        "用 WGS84 矩形框住澳门半岛、氹仔、路凼、路环和紧邻水域。计算前投影到 EPSG:32649（UTM 49N），因为像元大小和面积要用米。",
    )
    pdf.step(
        "2",
        "选影像",
        "两景都是 Sentinel-2C、同一 MGRS 分幅 49QGE，减少传感器和几何差异。产品级别是 L2A：欧空局已用 Sen2Cor 做成地表反射率（BOA），本项目没有再做大气校正。数据经 Earth Search 的 COG 按窗口读取，没有下整景。",
    )
    pdf.step(
        "3",
        "换算反射率",
        "文件里存的是整数 DN。反射率 = DN × 0.0001 − 0.1。NDVI 对两个波段共同的乘数不敏感，对这个加性偏移敏感，所以必须先换算再做比值。",
    )
    pdf.step(
        "4",
        "云掩膜",
        "用 SCL 去掉无数据、饱和、云影、云、卷云和雪，保留暗像元、植被、非植被、水体和未分类。夏季整景云量 26.70%，研究区内晴空像元仍有 74.76%；冬季整景云量 0.02%，研究区晴空 99.89%。比较时用研究区内的掩膜，不单看 110 公里分幅的整景云量。",
    )
    pdf.step(
        "5",
        "算 NDVI",
        "只用 10 米的红光 B04 和近红外 B08。近红外窄波段 B8A 是 20 米，用它会把指数拉回 20 米。反射率重采样用双线性；SCL 是类别，用最近邻，避免类别号被平均成没有意义的小数。",
    )
    pdf.step(
        "6",
        "做差和分区",
        "差值 = 夏季 NDVI − 冬季 NDVI，只保留两期都有效的像元。再用三个手绘矩形做分区统计。QGIS 用同一公式重算，用来核对 Python。重叠像元 RMSE 为 0。",
    )

    pdf.heading("三、NDVI 怎么解释")
    pdf.body(
        "NDVI = (B08 − B04) / (B08 + B04)。叶片强吸收红光、强反射近红外，比值接近 1；水体两个波段都低，或红光相对更高，比值接近 0 或为负。分母消掉两个波段一起变亮或变暗的部分。"
    )
    pdf.body(
        "先前 20 米结果发糊，是因为一次读取约 1.5 MB 的压缩瓦片会被截断，只能改读概视图，等于把相邻 4 个 10 米像元合成 1 个。现在按不超过 1 MB 的分段请求把瓦片下全，再在本地解开 Deflate。像元数从 222072 增到 888261，面积仍约 88.83 km²。SCL 本身是 20 米，最近邻到 10 米后，云的边界仍是 20 米台阶。"
    )

    pdf.heading("四、10 米结果怎么说")
    pdf.body("研究区多边形内有 888261 个 10 米像元，约 88.83 km²。面积 = 像元数 × 100 m²。")
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.table(
        ["指标", "夏季 08-06", "冬季 01-08", "差值（夏−冬）"],
        [
            ["有效像元", "662653（74.60%）", "887044（99.86%）", "661701"],
            ["面积", "66.27 km²", "88.70 km²", "66.17 km²"],
            ["均值", "0.215", "−0.002", "0.093"],
            ["中位数", "0.117", "0.028", "0.068"],
        ],
        [usable * 0.22, usable * 0.28, usable * 0.26, usable * 0.24],
    )
    pdf.body("按当日 SCL 分开之后：")
    pdf.table(
        ["SCL", "夏季 NDVI 均值", "冬季 NDVI 均值"],
        [
            ["水体（6）", "−0.188", "−0.342"],
            ["非植被（5）", "0.135", "0.196"],
            ["植被（4）", "0.781", "0.749"],
        ],
        [usable * 0.34, usable * 0.33, usable * 0.33],
    )
    pdf.body(
        "水体为负，植被高于 0.7。冬季被标成植被的像元仍有 0.75，符合亚热带常绿植被。被夏季 SCL 标成植被的像元，相对同一位置的冬季 NDVI 平均高 0.142（中位数 0.106）。被标成水体的像元，差值平均也有 0.143（中位数 0.150）：冬季水体更负，海面上的正差值不能都讲成植被增加。"
    )
    pdf.body("三个分区是手绘矩形，用来对比结构，不是官方境界。季节比较看差值这一列。")
    pdf.table(
        ["分区", "夏季均值", "冬季均值", "差值均值"],
        [
            ["澳门半岛", "0.092", "−0.004", "0.086"],
            ["氹仔—路凼", "0.130", "−0.073", "0.081"],
            ["路环", "0.397", "0.063", "0.089"],
        ],
        [usable * 0.28, usable * 0.24, usable * 0.24, usable * 0.24],
    )
    pdf.body(
        "路环夏季均值 0.397，半岛 0.092，和丘陵植被、建成区的分布一致。两期都有效的像元里，差值大于 0.10 的有 265972 个，约 26.60 km²，占重叠像元 40.2%。±0.10 只是练习用的显示阈值，不是土地覆盖变化的法定标准。"
    )

    pdf.heading("五、被追问时可以直接说的限制")
    pdf.bullet("只有两个日期，分不开季节物候和施工、填海、绿化。")
    pdf.bullet("冬季太阳高度角约 41°，夏季约 70°。水体上的正差值里包含光照和水面反射的差别。")
    pdf.bullet("SCL 和 NDVI 分段都没有地面样本，不能说成分类精度。")
    pdf.bullet("10 米能分开街区和山体植被，分不开单株树。B04/B08 原生就是 10 米，再把格网变细只是插值，不会增加信息。")

    pdf.output(OUT)
    print(OUT)


if __name__ == "__main__":
    build()

import json

import requests
from bs4 import BeautifulSoup
from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.padding import Padding
from rich.style import Style
from rich.styled import Styled
from rich.text import Text

from logger import Logger

from . import calc_infenc, get_dc
from .exception import APIError
from .jobs.document import ChapterDocument
from .jobs.exam import ChapterExam
from .jobs.video import ChapterVideo
from .schema import AccountInfo, ChapterModel

TaskPointType = ChapterExam | ChapterVideo | ChapterDocument

# 接口-课程章节任务点状态
API_CHAPTER_POINT = "https://mooc1-api.chaoxing.com/job/myjobsnodesmap"

# 接口-课程章节卡片
API_CHAPTER_CARDS = "https://mooc1-api.chaoxing.com/gas/knowledge"


class ClassChapters:
    "课程章节"
    logger: Logger
    session: requests.Session
    acc: AccountInfo
    chapters: list[ChapterModel]
    # 课程参数
    courseid: int   # 课程 id
    name: str       # 课程名
    clazzid: int    # 班级 id
    cpi: int
    
    tui_index: int  # TUI 列表指针索引值

    def __init__(
        self,
        session: requests.Session,
        acc: AccountInfo,
        courseid: int,
        name: str,
        clazzid: int,
        cpi: int,
        chapter_lst: list[dict],
    ) -> None:
        self.session = session
        self.acc = acc
        self.courseid = courseid
        self.clazzid = clazzid
        self.name = name
        self.cpi = cpi
        self.logger = Logger("Chapters")
        self.logger.set_loginfo(self.acc.phone)
        self.tui_index = 0

        self.chapters = [
            ChapterModel(
                chapter_id=cha["id"],
                jobs=cha["jobcount"],
                index=cha["indexorder"],
                name=cha["name"].strip(),
                label=cha["label"],
                layer=cha["layer"],
                status=cha["status"],
                point_total=0,
                point_finished=0,
            )
            for cha in chapter_lst
        ]
        self.chapters.sort(key=lambda x: tuple(int(v) for v in x.label.split(".")))

    def __len__(self) -> int:
        return len(self.chapters)

    def __repr__(self) -> str:
        return f"<ClassChapters id={self.courseid} name={self.name} count={len(self)}>"

    def is_finished(self, index: int) -> bool:
        "判断当前章节的任务点是否全部完成"
        return (self.chapters[index].point_total > 0) and (
            self.chapters[index].point_total == self.chapters[index].point_finished
        )

    def set_tui_index(self, index: int):
        "设置 TUI 指针位置"
        self.tui_index = index
    
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        "渲染章节列表到 TUI"
        total = len(self.chapters)
        half_length = options.height // 2
        if self.tui_index - half_length < 0:
            _min = 0
            _max = min(total, options.height)
        elif self.tui_index + half_length > total:
            _min = total - options.height
            _max = total
        else:
            _min = self.tui_index - half_length
            _max = self.tui_index + half_length
        for ptr in range(_min, _max):
            chapter = self.chapters[ptr]
            # 判断是否已完成章节任务
            yield Group(
                Text("❱", style=Style(color="red", bold=True), end="") if ptr == self.tui_index else Text("", end=""),
                Padding(
                    Styled(
                        Group(
                            Text(f"{chapter.label}: ", style=Style(color="green", bold=True), end=""),
                            Text(
                                f"({chapter.point_finished}/{chapter.point_total}) {chapter.name}",
                                style=Style(
                                    color="green" if self.is_finished(ptr) else ("white" if chapter.point_finished == 0 else "yellow")
                                ),
                                end="",
                                overflow="ellipsis"
                            )
                        ),
                        style=Style(bold=ptr == self.tui_index)
                    ),
                    pad=(0, 0, 0, chapter.layer * 2),
                ),
            )

    def fetch_point_status(self) -> None:
        "拉取章节任务点状态"
        resp = self.session.post(
            API_CHAPTER_POINT,
            data={
                "view": "json",
                "nodes": ",".join(str(c.chapter_id) for c in self.chapters),
                "clazzid": self.clazzid,
                "time": get_dc(),
                "userid": self.acc.puid,
                "cpi": self.cpi,
                "courseid": self.courseid,
            },
        )
        resp.raise_for_status()
        json_content = resp.json()
        for c in self.chapters:
            point_data = json_content[str(c.chapter_id)]
            c.point_total = point_data["totalcount"]
            c.point_finished = point_data["finishcount"]
        self.logger.info("任务点状态已更新")

    def __getitem__(self, key: int) -> list[TaskPointType]:
        return self.fetch_points_by_index(key)
    
    def __len__(self) -> int:
        return len(self.chapters)
    
    def fetch_points_by_index(self, index: int) -> list[TaskPointType]:
        "以课程序号拉取对应“章节”的任务节点卡片资源"
        params = {
            "id": self.chapters[index].chapter_id,
            "courseid": self.courseid,
            "fields": "id,parentnodeid,indexorder,label,layer,name,begintime,createtime,lastmodifytime,status,jobUnfinishedCount,clickcount,openlock,card.fields(id,knowledgeid,title,knowledgeTitile,description,cardorder).contentcard(all)",
            "view": "json",
            "token": "4faa8662c59590c6f43ae9fe5b002b42",
            "_time": get_dc(),
        }
        resp = self.session.get(
            API_CHAPTER_CARDS, params={**params, "inf_enc": calc_infenc(params)}
        )
        resp.raise_for_status()
        content_json = resp.json()
        if len(content_json["data"]) == 0:
            self.logger.error(
                f"获取章节任务节点卡片失败 "
                f"[{self.chapters[index].label}:{self.chapters[index].name}(Id.{self.chapters[index].chapter_id})]"
            )
            raise APIError
        cards = content_json["data"][0]["card"]["data"]
        self.logger.info(
            f"获取章节任务节点卡片成功 共 {len(cards)} 个 "
            f"[{self.chapters[index].label}:{self.chapters[index].name}(Id.{self.chapters[index].chapter_id})]"
        )
        point_objs = []  # 任务点实例化列表
        # 遍历章节卡片
        for card_index, card in enumerate(cards):
            # 保护措施, 一些章节卡片不存在任务点 iframe 内容, 如纯文字 图片等, 故跳过
            if not card.get("description"):
                self.logger.warning(f"({card_index}) 卡片 iframe 不存在 {card}")
                continue
            inline_html = BeautifulSoup(card["description"], "lxml")
            points = inline_html.find_all("iframe")
            self.logger.debug(f"({card_index}) 解析卡片成功 共 {len(points)} 个任务点")
            for point_index, point in enumerate(points):  # 遍历任务点列表
                # 获取任务点类型 跳过不存在 Type 的任务点
                if "module" not in point.attrs:
                    self.logger.warning(
                        f"({card_index}, {point_index}) 任务点 type 不存在 {card['description']}"
                    )
                    continue
                point_type = point["module"]
                json_data = json.loads(point["data"])
                # 进行分类讨论任务点类型并做 ORM
                match point_type:
                    case "insertvideo":
                        # 视频任务点
                        point_objs.append(
                            ChapterVideo(
                                session=self.session,
                                acc=self.acc,
                                card_index=card_index,
                                course_id=self.courseid,
                                knowledge_id=self.chapters[index].chapter_id,
                                object_id=json_data["objectid"],
                                clazz_id=self.clazzid,
                                cpi=self.cpi,
                            )
                        )
                        self.logger.debug(
                            f"({card_index}, {point_index}) 视频任务点 schema: {json_data}"
                        )
                    case "work":
                        # 测验任务点
                        point_objs.append(
                            ChapterExam(
                                session=self.session,
                                acc=self.acc,
                                card_index=card_index,
                                course_id=self.courseid,
                                work_id=json_data["workid"],
                                school_id=json_data.get("schoolid"),
                                job_id=json_data["_jobid"],
                                knowledge_id=self.chapters[index].chapter_id,
                                clazz_id=self.clazzid,
                                cpi=self.cpi,
                            )
                        )
                        self.logger.debug(
                            f"({card_index}, {point_index}) 测验任务点 schema: {json_data}"
                        )
                    case "insertdoc":
                        # 文档查看任务点
                        point_objs.append(
                            ChapterDocument(
                                session=self.session,
                                acc=self.acc,
                                card_index=card_index,
                                course_id=self.courseid,
                                knowledge_id=self.chapters[index].chapter_id,
                                clazz_id=self.clazzid,
                                cpi=self.cpi,
                                object_id=json_data["objectid"],
                            )
                        )
                        self.logger.debug(
                            f"({card_index}, {point_index}) 文档任务点 schema: {json_data}"
                        )
        self.logger.info(
            f"章节 任务节点解析成功 共 {len(point_objs)} 个 "
            f"[{self.chapters[index].label}:{self.chapters[index].name}(Id.{self.chapters[index].chapter_id})]"
        )
        return point_objs


__all__ = ["ClassChapters"]

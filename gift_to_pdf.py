#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Скрипт для конвертации GIFT формата в красивый PDF документ
"""

import re
import html
from pathlib import Path
from typing import List, Dict, Any

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    import os
    HAS_REPORTLAB = True
except ImportError:
    HAS_REPORTLAB = False
    print("ReportLab не установлен. Устанавливаю...")
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "reportlab"])
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
    from reportlab.lib.colors import HexColor
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    import os
    HAS_REPORTLAB = True


class GiftParser:
    """Парсер для GIFT формата"""
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.questions = []
        self.current_category = "Общие вопросы"
        
    def parse(self) -> List[Dict[str, Any]]:
        """Парсит GIFT файл и возвращает список вопросов"""
        # Пробуем разные кодировки
        encodings = ['utf-8', 'utf-8-sig', 'cp1251', 'windows-1251', 'latin-1']
        content = None
        
        for encoding in encodings:
            try:
                with open(self.file_path, 'r', encoding=encoding) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, LookupError):
                continue
        
        if content is None:
            raise ValueError(f"Не удалось прочитать файл {self.file_path} ни в одной из кодировок: {encodings}")
        
        # Удаляем комментарии
        content = self._remove_comments(content)
        
        # Разделяем на блоки по пустым строкам или по началу нового вопроса
        # В GIFT вопросы могут быть разделены пустыми строками или начинаться с {
        blocks = re.split(r'\n\s*\n', content)
        
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            
            # Проверяем категорию
            if block.startswith('$CATEGORY:'):
                self.current_category = block.replace('$CATEGORY:', '').strip()
                continue
            
            # Пропускаем другие директивы
            if block.startswith('$'):
                continue
            
            # Парсим вопрос
            question = self._parse_question(block)
            if question:
                question['category'] = self.current_category
                self.questions.append(question)
        
        return self.questions
    
    def _remove_comments(self, content: str) -> str:
        """Удаляет комментарии из GIFT файла"""
        # Удаляем однострочные комментарии //
        content = re.sub(r'//.*$', '', content, flags=re.MULTILINE)
        # Удаляем многострочные комментарии /* ... */
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        return content
    
    def _parse_question(self, block: str) -> Dict[str, Any]:
        """Парсит один вопрос"""
        question = {
            'type': 'unknown',
            'text': '',
            'answers': [],
            'category': self.current_category,
            'title': None,
            'feedback': None
        }
        
        # Извлекаем название вопроса (если есть)
        title_match = re.match(r'::(.*?)::', block)
        if title_match:
            question['title'] = title_match.group(1).strip()
            block = block[title_match.end():].strip()
        
        # Проверяем тип вопроса
        is_html = '[html]' in block
        if is_html:
            block = block.replace('[html]', '').strip()
        
        # Описание (description) - вопрос без ответов
        if not '{' in block:
            question['type'] = 'description'
            question['text'] = block.strip()
            question['answers'] = []
            return question
        
        # Ищем текст вопроса (до первой {)
        match = re.match(r'^(.*?)\{(.*)\}$', block, re.DOTALL)
        if not match:
            return None
        
        question_text = match.group(1).strip()
        answers_block = match.group(2).strip()
        
        question['text'] = question_text
        
        # Парсим ответы
        # True/False
        if answers_block in ['T', 'F', 'TRUE', 'FALSE']:
            question['type'] = 'truefalse'
            question['correct'] = answers_block.upper() in ['T', 'TRUE']
            question['answers'] = []
        # Числовой ответ (numerical)
        elif re.match(r'^#\s*[\d\.\-\+]+', answers_block):
            question['type'] = 'numerical'
            question['answers'] = self._parse_numerical(answers_block)
        # Сопоставление (проверяем первым, так как может содержать =)
        elif '->' in answers_block:
            question['type'] = 'matching'
            question['answers'] = self._parse_matching(answers_block)
        # Множественный выбор (если есть переносы строк или начинается с = или ~)
        elif '\n' in answers_block or answers_block.startswith('=') or answers_block.startswith('~') or answers_block.startswith('%'):
            question['type'] = 'multichoice'
            question['answers'] = self._parse_multichoice(answers_block)
        # Краткий ответ (все остальное - обычно это =ответ1 =ответ2 на одной строке)
        else:
            question['type'] = 'shortanswer'
            # Разделяем по = и берем все части после первого =
            parts = answers_block.split('=')
            answers = [part.strip() for part in parts[1:] if part.strip()]
            question['answers'] = [{'text': a, 'correct': True} for a in answers]
        
        return question
    
    def _parse_numerical(self, answers_block: str) -> List[Dict[str, Any]]:
        """Парсит числовые ответы"""
        answers = []
        # Формат: #число или #число:допустимая_погрешность
        # Может быть несколько вариантов: #10:2 #20:0.5
        matches = re.findall(r'#\s*([\d\.\-\+]+)(?::([\d\.]+))?', answers_block)
        for match in matches:
            value = float(match[0])
            tolerance = float(match[1]) if match[1] else 0
            answers.append({
                'value': value,
                'tolerance': tolerance,
                'text': f"{value}" + (f" ± {tolerance}" if tolerance > 0 else "")
            })
        return answers
    
    def _parse_multichoice(self, answers_block: str) -> List[Dict[str, Any]]:
        """Парсит варианты ответов для множественного выбора"""
        answers = []
        # Если это одна строка с несколькими ответами, разделяем по пробелу и = или ~
        if '\n' not in answers_block:
            # Одна строка: =ответ1 ~ответ2 =ответ3
            # Может быть с процентами: =ответ1#100% ~ответ2#0%
            parts = re.split(r'([=~%])', answers_block)
            current_marker = None
            current_text = ""
            for part in parts:
                if part in ['=', '~', '%']:
                    if current_marker and current_text:
                        # Парсим процент и обратную связь
                        answer_data = self._parse_answer_with_feedback(current_text.strip(), current_marker == '=' or current_marker == '%')
                        answers.append(answer_data)
                    current_marker = part
                    current_text = ""
                else:
                    current_text += part
            if current_marker and current_text:
                answer_data = self._parse_answer_with_feedback(current_text.strip(), current_marker == '=' or current_marker == '%')
                answers.append(answer_data)
        else:
            # Многострочный формат
            lines = answers_block.split('\n')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                if line.startswith('='):
                    text = line[1:].strip()
                    answer_data = self._parse_answer_with_feedback(text, True)
                    answers.append(answer_data)
                elif line.startswith('~'):
                    text = line[1:].strip()
                    answer_data = self._parse_answer_with_feedback(text, False)
                    answers.append(answer_data)
                elif line.startswith('%'):
                    text = line[1:].strip()
                    answer_data = self._parse_answer_with_feedback(text, True)
                    answers.append(answer_data)
        
        return answers
    
    def _parse_answer_with_feedback(self, text: str, is_correct: bool) -> Dict[str, Any]:
        """Парсит ответ с обратной связью и процентами"""
        # Формат: текст#обратная_связь или текст#100% или текст#100%#обратная_связь
        answer = {
            'text': text,
            'correct': is_correct,
            'percentage': None,
            'feedback': None
        }
        
        # Ищем процент и обратную связь
        parts = text.split('#')
        if len(parts) > 1:
            answer['text'] = parts[0].strip()
            # Проверяем, является ли второй элемент процентом
            percentage_match = re.match(r'^(\d+(?:\.\d+)?)%$', parts[1].strip())
            if percentage_match:
                answer['percentage'] = float(percentage_match.group(1))
                if len(parts) > 2:
                    answer['feedback'] = '#'.join(parts[2:]).strip()
            else:
                # Это обратная связь без процента
                answer['feedback'] = '#'.join(parts[1:]).strip()
        
        return answer
    
    def _parse_matching(self, answers_block: str) -> List[Dict[str, Any]]:
        """Парсит варианты для сопоставления"""
        matches = []
        lines = answers_block.split('\n')
        for line in lines:
            line = line.strip()
            if not line or not line.startswith('='):
                continue
            
            # Убираем = в начале
            line = line[1:].strip()
            if '->' in line:
                parts = line.split('->', 1)
                matches.append({
                    'left': parts[0].strip(),
                    'right': parts[1].strip()
                })
        
        return matches


class PDFGenerator:
    """Генератор PDF из вопросов"""
    
    def __init__(self, questions: List[Dict[str, Any]]):
        self.questions = questions
        self.styles = getSampleStyleSheet()
        self._register_fonts()
        self._setup_styles()
    
    def _register_fonts(self):
        """Регистрирует шрифты с поддержкой кириллицы"""
        try:
            # Пробуем использовать системные шрифты Windows
            fonts_paths = [
                r'C:\Windows\Fonts\arial.ttf',
                r'C:\Windows\Fonts\arialbd.ttf',
                r'C:\Windows\Fonts\ariali.ttf',
                r'C:\Windows\Fonts\arialbi.ttf',
            ]
            
            # Регистрируем Arial (обычный)
            if os.path.exists(fonts_paths[0]):
                pdfmetrics.registerFont(TTFont('ArialRU', fonts_paths[0]))
                if os.path.exists(fonts_paths[1]):
                    pdfmetrics.registerFont(TTFont('ArialRU-Bold', fonts_paths[1]))
                else:
                    pdfmetrics.registerFont(TTFont('ArialRU-Bold', fonts_paths[0]))
                self.font_name = 'ArialRU'
                self.font_bold = 'ArialRU-Bold'
                print("Используется шрифт Arial для кириллицы")
            else:
                # Пробуем Times New Roman
                times_paths = [
                    r'C:\Windows\Fonts\times.ttf',
                    r'C:\Windows\Fonts\timesbd.ttf',
                ]
                if os.path.exists(times_paths[0]):
                    pdfmetrics.registerFont(TTFont('TimesRU', times_paths[0]))
                    if os.path.exists(times_paths[1]):
                        pdfmetrics.registerFont(TTFont('TimesRU-Bold', times_paths[1]))
                    else:
                        pdfmetrics.registerFont(TTFont('TimesRU-Bold', times_paths[0]))
                    self.font_name = 'TimesRU'
                    self.font_bold = 'TimesRU-Bold'
                    print("Используется шрифт Times New Roman для кириллицы")
                else:
                    # Используем встроенный Unicode шрифт
                    pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))
                    self.font_name = 'HeiseiMin-W3'
                    self.font_bold = 'HeiseiMin-W3'
                    print("Используется встроенный Unicode шрифт")
        except Exception as e:
            print(f"Ошибка при регистрации шрифтов: {e}")
            # Если не удалось зарегистрировать, используем встроенный
            try:
                pdfmetrics.registerFont(UnicodeCIDFont('HeiseiMin-W3'))
                self.font_name = 'HeiseiMin-W3'
                self.font_bold = 'HeiseiMin-W3'
                print("Используется встроенный Unicode шрифт (fallback)")
            except:
                # Последний вариант - используем стандартный, но с правильной кодировкой
                self.font_name = 'Helvetica'
                self.font_bold = 'Helvetica-Bold'
                print("ВНИМАНИЕ: Используется Helvetica - кириллица может не отображаться!")
    
    def _setup_styles(self):
        """Настраивает стили для PDF"""
        # Заголовок документа
        self.title_style = ParagraphStyle(
            'CustomTitle',
            parent=self.styles['Heading1'],
            fontSize=24,
            textColor=HexColor('#2c3e50'),
            spaceAfter=30,
            alignment=TA_CENTER,
            fontName=self.font_bold
        )
        
        # Заголовок категории
        self.category_style = ParagraphStyle(
            'CategoryStyle',
            parent=self.styles['Heading2'],
            fontSize=16,
            textColor=HexColor('#FFFFFF'),
            backColor=HexColor('#667eea'),
            spaceAfter=12,
            spaceBefore=20,
            leftIndent=10,
            rightIndent=10,
            leading=20,
            fontName=self.font_bold
        )
        
        # Номер вопроса
        self.question_num_style = ParagraphStyle(
            'QuestionNum',
            parent=self.styles['Normal'],
            fontSize=12,
            textColor=HexColor('#3498db'),
            spaceAfter=6,
            fontName=self.font_bold
        )
        
        # Текст вопроса
        self.question_style = ParagraphStyle(
            'QuestionStyle',
            parent=self.styles['Normal'],
            fontSize=14,
            textColor=HexColor('#2c3e50'),
            spaceAfter=12,
            leading=20,
            leftIndent=10,
            fontName=self.font_name
        )
        
        # Правильный ответ
        self.correct_answer_style = ParagraphStyle(
            'CorrectAnswer',
            parent=self.styles['Normal'],
            fontSize=12,
            textColor=HexColor('#28a745'),
            spaceAfter=6,
            leftIndent=20,
            backColor=HexColor('#d4edda'),
            leading=16,
            fontName=self.font_name
        )
        
        # Неправильный ответ
        self.incorrect_answer_style = ParagraphStyle(
            'IncorrectAnswer',
            parent=self.styles['Normal'],
            fontSize=12,
            textColor=HexColor('#dc3545'),
            spaceAfter=6,
            leftIndent=20,
            backColor=HexColor('#f8d7da'),
            leading=16,
            fontName=self.font_name
        )
        
        # Сопоставление
        self.matching_style = ParagraphStyle(
            'MatchingStyle',
            parent=self.styles['Normal'],
            fontSize=12,
            spaceAfter=6,
            leftIndent=20,
            leading=16,
            fontName=self.font_name
        )
        
        # True/False
        self.truefalse_style = ParagraphStyle(
            'TrueFalseStyle',
            parent=self.styles['Normal'],
            fontSize=12,
            textColor=HexColor('#0066cc'),
            spaceAfter=12,
            leftIndent=20,
            backColor=HexColor('#e7f3ff'),
            leading=18,
            fontName=self.font_bold
        )
        
        # Краткий ответ
        self.shortanswer_style = ParagraphStyle(
            'ShortAnswerStyle',
            parent=self.styles['Normal'],
            fontSize=12,
            spaceAfter=12,
            leftIndent=20,
            backColor=HexColor('#fff3cd'),
            leading=18,
            fontName=self.font_name
        )
        
        # Футер
        self.footer_style = ParagraphStyle(
            'FooterStyle',
            parent=self.styles['Normal'],
            fontSize=10,
            textColor=HexColor('#6c757d'),
            alignment=TA_CENTER,
            spaceBefore=30,
            fontName=self.font_name
        )
    
    def _clean_html(self, text: str) -> str:
        """Очищает и конвертирует HTML теги в формат ReportLab"""
        # Заменяем экранированные символы
        text = text.replace('\\:', ':')
        text = text.replace('\\=', '=')
        text = text.replace('\\{', '{')
        text = text.replace('\\}', '}')
        # Удаляем теги <p> и </p>
        text = text.replace('<p>', '')
        text = text.replace('</p>', '<br/>')
        text = text.replace('&nbsp;', ' ')
        # Экранируем только специальные символы XML/HTML, но не кириллицу
        # ReportLab требует экранирования только <, >, &
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        # Возвращаем обратно теги, которые мы хотим сохранить
        text = text.replace('&lt;b&gt;', '<b>')
        text = text.replace('&lt;/b&gt;', '</b>')
        text = text.replace('&lt;i&gt;', '<i>')
        text = text.replace('&lt;/i&gt;', '</i>')
        text = text.replace('&lt;br/&gt;', '<br/>')
        text = text.replace('&lt;br&gt;', '<br/>')
        return text.strip()
    
    def generate_pdf(self, output_path: str):
        """Генерирует PDF файл"""
        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2*cm,
            bottomMargin=2*cm
        )
        
        story = []
        
        # Заголовок
        story.append(Paragraph("Тестовые вопросы по цифровой грамотности", self.title_style))
        story.append(Spacer(1, 0.5*cm))
        
        current_category = None
        question_num = 1
        
        for question in self.questions:
            # Добавляем заголовок категории
            if question['category'] != current_category:
                if current_category is not None:
                    story.append(Spacer(1, 0.3*cm))
                current_category = question['category']
                # Экранируем только XML символы, не кириллицу
                cat_text = current_category.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(cat_text, self.category_style))
                story.append(Spacer(1, 0.2*cm))
            
            # Номер вопроса
            story.append(Paragraph(f"<b>Вопрос {question_num}</b>", self.question_num_style))
            
            # Текст вопроса
            question_text = self._clean_html(question['text'])
            story.append(Paragraph(question_text, self.question_style))
            story.append(Spacer(1, 0.2*cm))
            
            # Добавляем ответы в зависимости от типа
            if question['type'] == 'multichoice':
                for answer in question['answers']:
                    # Экранируем только XML символы
                    answer_text = answer['text'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    if answer['correct']:
                        story.append(Paragraph(f"<b>✓</b> {answer_text}", self.correct_answer_style))
                    else:
                        story.append(Paragraph(f"<b>✗</b> {answer_text}", self.incorrect_answer_style))
            
            elif question['type'] == 'matching':
                for match in question['answers']:
                    left = match['left'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    right = match['right'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(f"<b>{left}</b> → {right}", self.matching_style))
            
            elif question['type'] == 'truefalse':
                answer_text = 'Верно' if question['correct'] else 'Неверно'
                story.append(Paragraph(f"Правильный ответ: {answer_text}", self.truefalse_style))
            
            elif question['type'] == 'shortanswer':
                answers_list = [a['text'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') for a in question['answers']]
                answers_text = ", ".join(answers_list)
                story.append(Paragraph(f"<b>Правильные ответы:</b> {answers_text}", self.shortanswer_style))
            
            elif question['type'] == 'numerical':
                story.append(Paragraph("<b>Числовой ответ:</b>", self.shortanswer_style))
                for answer in question['answers']:
                    answer_text = answer['text'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                    story.append(Paragraph(f"• {answer_text}", self.matching_style))
            
            elif question['type'] == 'description':
                # Описание - просто текст, без ответов
                pass
            
            story.append(Spacer(1, 0.3*cm))
            question_num += 1
        
        # Футер
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph(f"Документ сгенерирован из GIFT формата. Всего вопросов: {len(self.questions)}", self.footer_style))
        
        # Строим PDF
        doc.build(story)
        
        print(f"PDF успешно создан: {output_path}")


def main():
    import sys
    
    # Позволяем указать файл как аргумент командной строки
    if len(sys.argv) > 1:
        input_file = sys.argv[1]
    else:
        input_file = r"C:\Users\const\Untitled-1.ini"
    
    # Генерируем имя выходного файла
    if len(sys.argv) > 2:
        output_file = sys.argv[2]
    else:
        base_name = Path(input_file).stem
        output_file = str(Path(input_file).parent / f"{base_name}.pdf")
    
    print(f"Парсинг GIFT файла: {input_file}")
    try:
        parser = GiftParser(input_file)
        questions = parser.parse()
        
        print(f"Найдено вопросов: {len(questions)}")
        
        if len(questions) == 0:
            print("ВНИМАНИЕ: Не найдено ни одного вопроса!")
            return
        
        print("Генерация PDF...")
        generator = PDFGenerator(questions)
        generator.generate_pdf(output_file)
        
        print(f"Готово! PDF сохранен: {output_file}")
    except Exception as e:
        print(f"ОШИБКА: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()

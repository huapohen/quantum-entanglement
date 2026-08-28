package migrations

import "strings"

func validMigrationStatements(sql string) bool {
	heads, ok := migrationStatementHeads(sql)
	if !ok || len(heads) == 0 {
		return false
	}
	for _, head := range heads {
		if !allowedMigrationStatement(head) {
			return false
		}
	}
	return true
}

func allowedMigrationStatement(head []string) bool {
	if len(head) < 2 {
		return false
	}
	switch head[0] {
	case "ALTER":
		return head[1] == "TABLE"
	case "CREATE":
		if head[1] == "UNIQUE" {
			return len(head) >= 3 && head[2] == "INDEX"
		}
		return head[1] == "INDEX" || head[1] == "POLICY" || head[1] == "SCHEMA" ||
			head[1] == "TABLE"
	case "DROP":
		return head[1] == "INDEX" || head[1] == "POLICY" || head[1] == "SCHEMA" ||
			head[1] == "TABLE"
	default:
		return false
	}
}

func migrationStatementHeads(sql string) ([][]string, bool) {
	heads := make([][]string, 0)
	current := make([]string, 0, 3)
	for offset := 0; offset < len(sql); {
		switch {
		case sql[offset] == ';':
			if len(current) != 0 {
				heads = append(heads, current)
				current = make([]string, 0, 3)
			}
			offset++
		case isSQLSpace(sql[offset]):
			offset++
		case offset+1 < len(sql) && sql[offset] == '-' && sql[offset+1] == '-':
			offset += 2
			for offset < len(sql) && sql[offset] != '\n' {
				offset++
			}
		case offset+1 < len(sql) && sql[offset] == '/' && sql[offset+1] == '*':
			var ok bool
			offset, ok = skipSQLBlockComment(sql, offset)
			if !ok {
				return nil, false
			}
		case sql[offset] == '\'':
			var ok bool
			offset, ok = skipSQLQuoted(sql, offset, '\'')
			if !ok {
				return nil, false
			}
		case sql[offset] == '"':
			var ok bool
			offset, ok = skipSQLQuoted(sql, offset, '"')
			if !ok {
				return nil, false
			}
		case sql[offset] == '$':
			next, matched, ok := skipSQLDollarQuote(sql, offset)
			if !ok {
				return nil, false
			}
			if matched {
				offset = next
			} else {
				offset++
			}
		case isSQLIdentifierStart(sql[offset]):
			start := offset
			offset++
			for offset < len(sql) && isSQLIdentifierPart(sql[offset]) {
				offset++
			}
			if len(current) < 3 {
				current = append(current, strings.ToUpper(sql[start:offset]))
			}
		default:
			offset++
		}
	}
	if len(current) != 0 {
		heads = append(heads, current)
	}
	return heads, true
}

func skipSQLBlockComment(sql string, offset int) (int, bool) {
	depth := 1
	offset += 2
	for offset < len(sql) {
		switch {
		case offset+1 < len(sql) && sql[offset] == '/' && sql[offset+1] == '*':
			depth++
			offset += 2
		case offset+1 < len(sql) && sql[offset] == '*' && sql[offset+1] == '/':
			depth--
			offset += 2
			if depth == 0 {
				return offset, true
			}
		default:
			offset++
		}
	}
	return offset, false
}

func skipSQLQuoted(sql string, offset int, delimiter byte) (int, bool) {
	offset++
	for offset < len(sql) {
		if sql[offset] != delimiter {
			offset++
			continue
		}
		if offset+1 < len(sql) && sql[offset+1] == delimiter {
			offset += 2
			continue
		}
		return offset + 1, true
	}
	return offset, false
}

func skipSQLDollarQuote(sql string, offset int) (int, bool, bool) {
	tagEnd := offset + 1
	for tagEnd < len(sql) && isSQLDollarTagPart(sql[tagEnd]) {
		tagEnd++
	}
	if tagEnd >= len(sql) || sql[tagEnd] != '$' {
		return offset, false, true
	}
	tag := sql[offset : tagEnd+1]
	closingOffset := strings.Index(sql[tagEnd+1:], tag)
	if closingOffset < 0 {
		return len(sql), true, false
	}
	return tagEnd + 1 + closingOffset + len(tag), true, true
}

func isSQLSpace(value byte) bool {
	return value == ' ' || value == '\t' || value == '\n' || value == '\r' || value == '\f'
}

func isSQLIdentifierStart(value byte) bool {
	return value == '_' || value >= 'A' && value <= 'Z' || value >= 'a' && value <= 'z'
}

func isSQLIdentifierPart(value byte) bool {
	return isSQLIdentifierStart(value) || value >= '0' && value <= '9' || value == '$'
}

func isSQLDollarTagPart(value byte) bool {
	return value == '_' || value >= 'A' && value <= 'Z' || value >= 'a' && value <= 'z' ||
		value >= '0' && value <= '9'
}

/** Localized copy for VideoForge-owned identity surfaces. */
export const en = {
  more: 'More', settings: 'Settings', feedback: 'Feedback', register: 'Create my account',
  runtime: 'Runtime & dependencies',
  login: 'Sign in', account: 'My VideoForge account', close: 'Close', nickname: 'Nickname',
  identifier: 'Phone number or email', code: 'Verification code', send: 'Send code',
  sending: 'Sending…', verify: 'Verify and sign in', verifying: 'Verifying…', signout: 'Sign out',
  unavailable: 'Account registration is not available yet. Please contact the administrator.',
  loading: 'Loading…', retry: 'Retry', failed: 'Unable to connect to the account service.',
  sent: 'Code sent to', wait: 'Resend in', seconds: 's', phone: 'Phone', email: 'Email',
  registerNote: 'Create an account with your nickname and verified phone number or email.',
  loginNote: 'Sign in with a code sent to your registered phone number or email.',
  users: 'Registered users', refresh: 'Refresh', previous: 'Previous', next: 'Next',
  role: 'Role', created: 'Registered', lastLogin: 'Last sign-in', total: 'Total users',
  empty: 'No registered users', required: 'Enter a valid contact and verification code.',
  nicknameRequired: 'Enter a nickname to create an account.', admin: 'Administrator', member: 'User',
  expired: 'Your session has expired. Sign in again.', useLogin: 'Already registered? Sign in',
  useRegister: 'New here? Create my account', feedbackNote: 'Send feedback to the application owner.',
} as const

export const zh: Record<keyof typeof en, string> = {
  more: '更多', settings: '设置', feedback: '意见反馈', register: '注册我的账户',
  runtime: '运行环境',
  login: '登录', account: '我的 VideoForge 账户', close: '关闭', nickname: '昵称',
  identifier: '手机号或邮箱', code: '验证码', send: '获取验证码', sending: '发送中…',
  verify: '验证并登录', verifying: '验证中…', signout: '退出登录',
  unavailable: '账户服务尚未开通，请联系管理员。', loading: '加载中…', retry: '重试',
  failed: '暂时无法连接账户服务。', sent: '验证码已发送至', wait: '重新发送还需', seconds: '秒',
  phone: '手机号', email: '邮箱', registerNote: '填写昵称，并验证手机号或邮箱后注册账户。',
  loginNote: '使用已注册的手机号或邮箱接收验证码登录。', users: '注册用户管理',
  refresh: '刷新', previous: '上一页', next: '下一页', role: '身份', created: '注册时间',
  lastLogin: '最近登录', total: '用户总数', empty: '暂无注册用户',
  required: '请填写有效的手机号或邮箱及验证码。', nicknameRequired: '注册时请填写昵称。',
  admin: '管理员', member: '用户', expired: '登录已过期，请重新登录。',
  useLogin: '已有账户？登录', useRegister: '还没有账户？注册我的账户',
  feedbackNote: '请将使用反馈提供给软件所有者。',
}

export type AccountCopyKey = keyof typeof en
